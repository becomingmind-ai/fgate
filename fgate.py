#!/usr/bin/env python3
"""Fgate: cheap worker/reviewer execution with a compact Codex exit gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


SNIPPET_LIMIT = 32 * 1024
CONTEXT_LIMIT = 128 * 1024
LARGE_FILE = 2 * 1024 * 1024
OUTPUT_LIMIT = 20_000
ROLE_FILE_LIMIT = 256 * 1024
CHECK_OUTPUT_LIMIT = 20_000
SOL_CHECK_OUTPUT_LIMIT = 4_000
DEFAULT_TIMEOUT = 120
TOOL_MARKERS = ("<｜DSML｜tool_calls>", "<tool_calls>", "<tool_call>")
DECISIONS = {"RECOMMEND_PASS", "REWORK", "ESCALATE"}

SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token)\b"
                r"(\s*[=:]\s*)([^\s,;]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\bauthorization\b(\s*[=:]\s*)([^\r\n]+)"),
     r"authorization\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_OPENAI_KEY]"),
)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def value_hash(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def bounded(text: str, limit: int) -> str:
    encoded = text.encode()
    return text if len(encoded) <= limit else \
        encoded[:limit].decode(errors="ignore") + "\n[truncated]"


def clean_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20 or not all(
            isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be at most 20 strings")
    return [bounded(redact(item), 2000) for item in value]


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: dict, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if overwrite else "x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def resolve(workspace: Path, value: str) -> Path:
    target = (workspace / value).resolve()
    if target != workspace and workspace not in target.parents:
        raise ValueError(f"path escapes workspace: {value}")
    return target


def require(value: dict, fields: tuple[tuple[str, type], ...], label: str) -> None:
    for name, kind in fields:
        if not isinstance(value.get(name), kind):
            raise ValueError(f"{label} {name} is required")


def profile_workflow(profile: object, label: str) -> str:
    if not isinstance(profile, dict):
        raise ValueError(f"{label} profile is required")
    workflow = profile.get("workflow", "full")
    if workflow not in ("full", "lite"):
        raise ValueError(f"{label} workflow must be full or lite")
    if workflow == "lite":
        if label != "worker" or profile.get("provider") != "codex-subagent" or \
                not isinstance(profile.get("model_id"), str) or not profile["model_id"]:
            raise ValueError("lite is only valid for a named Codex subagent worker")
        if "command" in profile:
            raise ValueError("lite Codex subagent must not define a subprocess command")
        return workflow
    command = profile.get("command")
    if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command):
        raise ValueError(f"{label} command must be a nonempty argv array")
    joined = "\n".join(command)
    if "{input}" not in joined or "{output}" not in joined:
        raise ValueError(f"{label} command requires {{input}} and {{output}}")
    return workflow


def load_project(path: Path) -> tuple[dict, Path]:
    project = read_json(path)
    if project.get("schema") != "fgate-project-v1":
        raise ValueError("project schema must be fgate-project-v1")
    require(project, (("project", str), ("workspace", str), ("active_worker", str),
                      ("workers", dict), ("active_reviewer", str),
                      ("reviewers", dict), ("checks", dict), ("harness", list),
                      ("policy", dict), ("requires_weight_reload", bool)), "project")
    if project.get("supervisor") != "codex-default":
        raise ValueError("project supervisor must be codex-default")
    workspace = (path.resolve().parent / project["workspace"]).resolve()
    if not workspace.is_dir():
        raise ValueError("project workspace is not a directory")
    profile_workflow(project["workers"].get(project["active_worker"]), "worker")
    profile_workflow(project["reviewers"].get(project["active_reviewer"]), "reviewer")
    policy = project["policy"]
    if policy.get("automatic_retry") is not False or \
            not isinstance(policy.get("max_rework"), int) or \
            policy["max_rework"] < 0 or policy.get("codex_gate_required") is not True:
        raise ValueError("policy requires automatic_retry=false, max_rework>=0 and Codex gate")
    if not project["harness"]:
        raise ValueError("project harness files are required")
    return project, workspace


def load_task(path: Path) -> dict:
    task = read_json(path)
    if task.get("schema") != "fgate-task-v1":
        raise ValueError("task schema must be fgate-task-v1")
    require(task, (("id", str), ("objective", str), ("hard_gates", list),
                   ("context", list), ("checks", list), ("risk", str),
                   ("worker_mode", str), ("requires_weight_reload", bool)), "task")
    if not task["id"] or not task["objective"] or not task["hard_gates"]:
        raise ValueError("task id, objective and hard_gates must be nonempty")
    if task["risk"] not in ("low", "medium", "high"):
        raise ValueError("task risk must be low, medium or high")
    if task["worker_mode"] not in ("read_only", "workspace_write"):
        raise ValueError("worker_mode must be read_only or workspace_write")
    if len(task["checks"]) != len(set(task["checks"])) or not all(
            isinstance(name, str) and name for name in task["checks"]):
        raise ValueError("task checks must be unique names")
    return task


def entries(values: list) -> list[dict]:
    result = []
    for value in values:
        if isinstance(value, str):
            result.append({"path": value})
        elif isinstance(value, dict) and isinstance(value.get("path"), str):
            result.append(value)
        else:
            raise ValueError("file entry requires path")
    return result


def snapshot(workspace: Path, values: list, include_text: bool = True) -> tuple[list, list]:
    identities, snippets, seen = [], [], set()
    total = 0
    for entry in entries(values):
        path = resolve(workspace, entry["path"])
        if not path.is_file():
            raise ValueError(f"file missing: {entry['path']}")
        relative, actual = str(path.relative_to(workspace)), file_hash(path)
        if entry.get("sha256") and entry["sha256"] != actual:
            raise ValueError(f"SHA mismatch: {relative}")
        identity = {"path": relative, "sha256": actual, "bytes": path.stat().st_size}
        if relative not in {item["path"] for item in identities}:
            identities.append(identity)
        start = max(1, int(entry.get("start_line", 1)))
        end = min(int(entry.get("end_line", start + 239)), start + 399)
        key = (relative, start, end)
        if key in seen:
            raise ValueError(f"duplicate file range: {relative}:{start}-{end}")
        seen.add(key)
        if not include_text or entry.get("binary") or identity["bytes"] > LARGE_FILE:
            continue
        with path.open("rb") as stream:
            if b"\0" in stream.read(4096):
                continue
        rows = []
        with path.open(errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if number > end:
                    break
                if number >= start:
                    rows.append(line.rstrip())
        text = bounded(redact("\n".join(rows)), SNIPPET_LIMIT)
        total += len(text.encode())
        if total > CONTEXT_LIMIT:
            raise ValueError("snapshot exceeds 128 KiB")
        snippets.append({"path": relative, "start_line": start,
                         "end_line": min(end, start + len(rows) - 1), "content": text})
    return identities, snippets


def profile(project: dict, role: str) -> tuple[str, dict]:
    name = project[f"active_{role}"]
    return name, project[f"{role}s"][name]


def make_packet(project_path: Path, task_path: Path) -> tuple[dict, dict, dict, Path]:
    project, workspace = load_project(project_path)
    task = load_task(task_path)
    missing = set(task["checks"]) - set(project["checks"])
    if missing:
        raise ValueError(f"unregistered checks: {sorted(missing)}")
    context_ids, context_text = snapshot(workspace, task["context"])
    harness_ids, harness_text = snapshot(workspace, project["harness"])
    worker_name, worker = profile(project, "worker")
    reviewer_name, reviewer = profile(project, "reviewer")
    workflow = profile_workflow(worker, "worker")
    packet = {
        "schema": "fgate-worker-packet-v1", "kind": "worker_packet",
        "project": project["project"], "task_id": task["id"], "version": task["id"],
        "objective": task["objective"], "hard_gates": task["hard_gates"],
        "constraints": task.get("constraints", []), "risk": task["risk"],
        "worker_mode": task["worker_mode"],
        "requires_weight_reload": task["requires_weight_reload"],
        "project_sha256": file_hash(project_path), "task_sha256": file_hash(task_path),
        "spec_sha256": file_hash(task_path), "context_identities": context_ids,
        "context_snippets": context_text, "harness_identities": harness_ids,
        "harness_snippets": harness_text, "harness_fingerprint": value_hash(harness_ids),
        "registered_checks": task["checks"],
        "worker": {"name": worker_name, "model_id": worker.get("model_id", worker_name),
                   "profile_sha256": value_hash(worker), "workflow": workflow},
        "reviewer": {"name": reviewer_name,
                     "model_id": reviewer.get("model_id", reviewer_name),
                     "profile_sha256": value_hash(reviewer)},
        "supervisor": "codex-default", "workflow": workflow,
        "policy": project["policy"],
        "output_contract": {
            "status": "complete or needs_help", "finish_reason": "stop when complete",
            "answer": "concise result, patch or evidence", "changes": "optional list",
            "help_request": "required when blocked, looping or uncertain",
        },
    }
    return packet, project, task, workspace


def verify_packet(project_path: Path, task_path: Path, packet_path: Path,
                  allow_context_change: bool = False) -> tuple[dict, dict, dict, Path]:
    packet = read_json(packet_path)
    expected, project, task, workspace = make_packet(project_path, task_path)
    if allow_context_change:
        expected["context_identities"] = packet.get("context_identities")
        expected["context_snippets"] = packet.get("context_snippets")
    if packet != expected:
        raise ValueError("packet identity, harness or frozen context drift")
    return packet, project, task, workspace


def prepare(project: Path, task: Path, output: Path, overwrite: bool) -> int:
    packet, _, _, _ = make_packet(project, task)
    write_json(output, packet, overwrite)
    return 0


def invoke(command: list[str], input_path: Path, workspace: Path, timeout: int) -> tuple[int, dict | None, str]:
    handle, name = tempfile.mkstemp(prefix="fgate-role-", suffix=".json")
    os.close(handle)
    raw_path = Path(name)
    raw_path.unlink()
    argv = [item.replace("{input}", str(input_path.resolve()))
            .replace("{output}", str(raw_path))
            .replace("{workspace}", str(workspace)) for item in command]
    try:
        run = subprocess.run(argv, cwd=workspace, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=timeout, check=False)
        if raw_path.is_file() and raw_path.stat().st_size > ROLE_FILE_LIMIT:
            return run.returncode, None, "role output file exceeds 256 KiB"
        raw = read_json(raw_path) if raw_path.is_file() else None
        return run.returncode, raw, bounded(redact(run.stdout), 4000)
    except subprocess.TimeoutExpired as error:
        raw = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) \
            else (error.stdout or "")
        return -1, None, bounded(redact(raw), 4000)
    finally:
        raw_path.unlink(missing_ok=True)


def run_worker(project_path: Path, task_path: Path, packet_path: Path,
               output: Path, overwrite: bool) -> int:
    packet, project, task, workspace = verify_packet(project_path, task_path, packet_path)
    name, worker = profile(project, "worker")
    if packet["workflow"] == "lite":
        raise ValueError("lite Codex subagent is externally orchestrated; use import-worker")
    timeout = min(max(1, int(worker.get("timeout_seconds", 900))), 3600)
    code, raw, process_output = invoke(worker["command"], packet_path, workspace, timeout)
    return write_worker_result(packet, project, task, workspace, name, worker, raw or {},
                               code, process_output, output, overwrite)


def write_worker_result(packet: dict, project: dict, task: dict, workspace: Path,
                        name: str, worker: dict, raw: dict, code: int,
                        process_output: str, output: Path, overwrite: bool) -> int:
    answer = raw.get("answer") or raw.get("final") or ""
    complete = code == 0 and raw.get("status") == "complete" and \
        raw.get("finish_reason") == "stop" and isinstance(answer, str) and bool(answer)
    if complete and (len(answer.encode()) > OUTPUT_LIMIT or
                     any(marker in answer for marker in TOOL_MARKERS)):
        complete = False
    post_ids, _ = snapshot(workspace, task["context"], include_text=False)
    current_harness, _ = snapshot(workspace, project["harness"], include_text=False)
    if value_hash(current_harness) != packet["harness_fingerprint"]:
        raise ValueError("worker modified governed harness files")
    if task["worker_mode"] == "read_only" and post_ids != packet["context_identities"]:
        raise ValueError("read-only worker modified task context")
    status = "complete" if complete else "needs_help"
    reason = None if complete else raw.get("help_request") or raw.get("error") or \
        (f"worker exited {code}: {process_output}" if code else "worker incomplete or malformed")
    result = {
        "schema": "fgate-worker-result-v1", "status": status,
        "finish_reason": "stop" if complete else raw.get("finish_reason"),
        "task_id": task["id"], "packet_sha256": value_hash(packet),
        "worker": {"name": name, "model_id": worker.get("model_id", name),
                   "profile_sha256": value_hash(worker)},
        "answer": redact(answer) if isinstance(answer, str) else "",
        "changes": clean_list(raw.get("changes", []), "worker changes"),
        "self_review": clean_list(raw.get("self_review", []), "worker self_review"),
        "usage": raw.get("usage", {}) if isinstance(raw.get("usage", {}), dict) else {},
        "post_context_identities": post_ids,
        "help_request": bounded(redact(str(reason or "")), 4000),
    }
    write_json(output, result, overwrite)
    return 0 if complete else 1


def import_worker(project_path: Path, task_path: Path, packet_path: Path,
                  handoff_path: Path, output: Path, overwrite: bool) -> int:
    packet, project, task, workspace = verify_packet(
        project_path, task_path, packet_path, allow_context_change=True)
    name, worker = profile(project, "worker")
    if packet["workflow"] != "lite":
        raise ValueError("import-worker requires a lite Codex subagent profile")
    handoff = read_json(handoff_path)
    if handoff.get("schema") != "fgate-lite-handoff-v1" or \
            handoff.get("packet_sha256") != value_hash(packet):
        raise ValueError("lite handoff schema or packet identity mismatch")
    return write_worker_result(packet, project, task, workspace, name, worker, handoff,
                               0, "", output, overwrite)


def check_definition(value: object) -> tuple[list[str], int]:
    if isinstance(value, list):
        argv, timeout = value, DEFAULT_TIMEOUT
    elif isinstance(value, dict):
        argv, timeout = value.get("argv"), int(value.get("timeout_seconds", DEFAULT_TIMEOUT))
    else:
        raise ValueError("check must be argv or an object")
    if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv):
        raise ValueError("check argv must be a nonempty string array")
    return argv, min(max(1, timeout), 3600)


def run_checks(project_path: Path, task_path: Path, packet_path: Path,
               worker_path: Path, output: Path, overwrite: bool) -> int:
    packet, project, task, workspace = verify_packet(
        project_path, task_path, packet_path, allow_context_change=True)
    worker = read_json(worker_path)
    if worker.get("schema") != "fgate-worker-result-v1" or \
            worker.get("packet_sha256") != value_hash(packet) or \
            worker.get("status") != "complete":
        raise ValueError("checks require a complete worker result")
    results, passed_all = {}, True
    for name in task["checks"]:
        argv, timeout = check_definition(project["checks"][name])
        try:
            run = subprocess.run(argv, cwd=workspace, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=timeout, check=False)
            code, timed_out, text = run.returncode, False, run.stdout
        except subprocess.TimeoutExpired as error:
            code, timed_out = -1, True
            text = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) \
                else (error.stdout or "")
        passed = code == 0 and not timed_out
        passed_all &= passed
        results[name] = {"status": "PASS" if passed else "FAIL", "exit_code": code,
                         "timed_out": timed_out, "argv_sha256": value_hash(argv),
                         "argv": [redact(item) for item in argv],
                         "output": bounded(redact(text), CHECK_OUTPUT_LIMIT)}
    evidence = {"schema": "fgate-check-evidence-v1", "task_id": task["id"],
                "packet_sha256": value_hash(packet), "worker_sha256": value_hash(worker),
                "status": "PASS" if passed_all else "FAIL", "checks": results}
    write_json(output, evidence, overwrite)
    return 0 if passed_all else 1


def valid_checks(project: dict, task: dict, packet: dict, worker: dict, evidence: dict) -> bool:
    if evidence.get("schema") != "fgate-check-evidence-v1" or \
            evidence.get("packet_sha256") != value_hash(packet) or \
            evidence.get("worker_sha256") != value_hash(worker) or \
            set(evidence.get("checks", {})) != set(task["checks"]):
        return False
    for name in task["checks"]:
        argv, _ = check_definition(project["checks"][name])
        item = evidence["checks"][name]
        if item.get("status") != "PASS" or item.get("exit_code") != 0 or \
                item.get("timed_out") is not False or \
                item.get("argv_sha256") != value_hash(argv):
            return False
    return evidence.get("status") == "PASS"


def make_review_packet(project: dict, task: dict, packet: dict, worker: dict,
                       evidence: dict, workspace: Path) -> dict:
    if not valid_checks(project, task, packet, worker, evidence):
        raise ValueError("review requires passing deterministic checks")
    current_ids, current_text = snapshot(workspace, task["context"])
    compact_checks = {name: {"status": item["status"], "exit_code": item["exit_code"],
        "output": bounded(item["output"], SOL_CHECK_OUTPUT_LIMIT)}
        for name, item in evidence["checks"].items()}
    return {
        "schema": "fgate-review-packet-v1", "task_id": task["id"],
        "objective": task["objective"], "hard_gates": task["hard_gates"],
        "constraints": task.get("constraints", []), "risk": task["risk"],
        "requires_weight_reload": task["requires_weight_reload"],
        "baseline_context": packet["context_identities"], "current_context": current_ids,
        "current_context_snippets": current_text, "worker": {
            "name": worker["worker"]["name"], "answer": worker["answer"],
            "changes": worker["changes"], "self_review": worker["self_review"]},
        "checks": compact_checks,
        "output_contract": {"decision": sorted(DECISIONS), "findings": "list",
                            "uncertainties": "list", "risk": "low, medium or high"},
        "instruction": "Review independently. Do not trust worker claims without evidence. "
                       "Return only the output contract JSON.",
    }


def reviewer_payload(raw: dict) -> dict:
    if raw.get("decision") in DECISIONS:
        return raw
    answer = raw.get("answer") or raw.get("final")
    if isinstance(answer, str):
        value = json.loads(answer)
        if isinstance(value, dict):
            return value
    raise ValueError("reviewer did not return exact JSON")


def review(project_path: Path, task_path: Path, packet_path: Path, worker_path: Path,
           checks_path: Path, output: Path, overwrite: bool) -> int:
    packet, project, task, workspace = verify_packet(
        project_path, task_path, packet_path, allow_context_change=True)
    if packet["workflow"] == "lite":
        raise ValueError("lite workflow skips the cheap reviewer; use finalize after checks")
    worker, evidence = read_json(worker_path), read_json(checks_path)
    review_packet = make_review_packet(project, task, packet, worker, evidence, workspace)
    handle, name = tempfile.mkstemp(prefix="fgate-review-", suffix=".json")
    os.close(handle)
    review_path = Path(name)
    write_json(review_path, review_packet, overwrite=True)
    reviewer_name, reviewer = profile(project, "reviewer")
    timeout = min(max(1, int(reviewer.get("timeout_seconds", 900))), 3600)
    before_context, _ = snapshot(workspace, task["context"], include_text=False)
    before_harness, _ = snapshot(workspace, project["harness"], include_text=False)
    try:
        code, raw, process_output = invoke(reviewer["command"], review_path, workspace, timeout)
    finally:
        review_path.unlink(missing_ok=True)
    after_context, _ = snapshot(workspace, task["context"], include_text=False)
    after_harness, _ = snapshot(workspace, project["harness"], include_text=False)
    if before_context != after_context or before_harness != after_harness:
        raise ValueError("reviewer modified governed files")
    try:
        payload = reviewer_payload(raw or {}) if code == 0 else {}
        decision = payload.get("decision")
        if decision not in DECISIONS:
            raise ValueError("invalid reviewer decision")
        findings = clean_list(payload.get("findings", []), "reviewer findings")
        uncertainties = clean_list(payload.get("uncertainties", []),
                                   "reviewer uncertainties")
        risk = payload.get("risk", task["risk"])
        if risk not in ("low", "medium", "high"):
            raise ValueError("invalid reviewer risk")
    except (ValueError, json.JSONDecodeError) as error:
        decision, findings, uncertainties, risk = "ESCALATE", [], [
            f"reviewer malformed or failed: {error}; process={process_output}"], task["risk"]
    result = {
        "schema": "fgate-review-result-v1", "task_id": task["id"],
        "review_packet_sha256": value_hash(review_packet),
        "packet_sha256": value_hash(packet), "worker_sha256": value_hash(worker),
        "checks_sha256": value_hash(evidence),
        "reviewer": {"name": reviewer_name,
                     "model_id": reviewer.get("model_id", reviewer_name),
                     "profile_sha256": value_hash(reviewer),
                     "independence": "different_model" if
                     reviewer.get("model_id", reviewer_name) !=
                     worker["worker"].get("model_id", worker["worker"]["name"])
                     else "same_model_fresh_context"},
        "decision": decision, "findings": findings, "uncertainties": uncertainties,
        "risk": risk, "usage": (raw or {}).get("usage", {}),
    }
    write_json(output, result, overwrite)
    return 0 if decision == "RECOMMEND_PASS" else 1


def finalize(project_path: Path, task_path: Path, packet_path: Path, worker_path: Path,
             output: Path, checks_path: Path | None, review_path: Path | None,
             overwrite: bool) -> int:
    packet, project, task, workspace = verify_packet(
        project_path, task_path, packet_path, allow_context_change=True)
    worker = read_json(worker_path)
    if worker.get("schema") != "fgate-worker-result-v1" or \
            worker.get("packet_sha256") != value_hash(packet):
        raise ValueError("worker result identity mismatch")
    gate = {
        "schema": "fgate-sol-gate-v1", "kind": "sol_decision_packet",
        "task_id": task["id"], "objective": task["objective"],
        "hard_gates": task["hard_gates"], "risk": task["risk"],
        "requires_weight_reload": task["requires_weight_reload"],
        "frozen_identity": {"project_sha256": packet["project_sha256"],
            "task_sha256": packet["task_sha256"], "packet_sha256": value_hash(packet),
            "harness_fingerprint": packet["harness_fingerprint"]},
        "worker": {"name": worker["worker"]["name"], "status": worker["status"],
            "summary": worker["answer"], "changes": worker["changes"],
            "usage": worker["usage"], "help_request": worker["help_request"]},
        "checks": {}, "cheap_review": None, "sol_decision": None,
        "sol_decision_owner": "codex-default",
    }
    if worker["status"] != "complete":
        gate.update({"status": "needs_sol_assistance",
                     "recommended_action": "ASSIST_OR_TAKEOVER"})
        write_json(output, gate, overwrite)
        return 0
    if checks_path is None:
        raise ValueError("complete worker requires --checks")
    evidence = read_json(checks_path)
    if not valid_checks(project, task, packet, worker, evidence):
        raise ValueError("deterministic checks did not pass")
    gate["checks"] = {name: {"status": item["status"], "exit_code": item["exit_code"],
        "output": bounded(item["output"], SOL_CHECK_OUTPUT_LIMIT)}
        for name, item in evidence["checks"].items()}
    if packet["workflow"] == "lite":
        if review_path is not None:
            raise ValueError("lite workflow must not include a cheap review")
        gate["cheap_review"] = {"status": "skipped", "reason": "lite workflow"}
        gate.update({"status": "ready_for_sol",
                     "recommended_action": "DEEP_REVIEW" if task["risk"] == "high" else "DECIDE",
                     "real_experiment": task.get("real_experiment", {"status": "not_run"}),
                     "lifecycle": task.get("lifecycle", {})})
        write_json(output, gate, overwrite)
        return 0
    if review_path is None:
        raise ValueError("full workflow requires --review")
    reviewer = read_json(review_path)
    expected_review = make_review_packet(project, task, packet, worker, evidence, workspace)
    if reviewer.get("schema") != "fgate-review-result-v1" or \
            reviewer.get("packet_sha256") != value_hash(packet) or \
            reviewer.get("worker_sha256") != value_hash(worker) or \
            reviewer.get("checks_sha256") != value_hash(evidence) or \
            reviewer.get("review_packet_sha256") != value_hash(expected_review):
        raise ValueError("review identity mismatch")
    gate["cheap_review"] = {key: reviewer[key] for key in
        ("reviewer", "decision", "findings", "uncertainties", "risk", "usage")}
    if reviewer["decision"] == "REWORK":
        status, action = "return_to_worker", "REWORK_ONCE"
    elif reviewer["decision"] == "ESCALATE" or reviewer["uncertainties"]:
        status, action = "needs_sol_assistance", "ASSIST_OR_TAKEOVER"
    elif task["risk"] == "high" or reviewer["risk"] == "high":
        status, action = "ready_for_sol", "DEEP_REVIEW"
    else:
        status, action = "ready_for_sol", "DECIDE"
    gate.update({"status": status, "recommended_action": action,
                 "real_experiment": task.get("real_experiment", {"status": "not_run"}),
                 "lifecycle": task.get("lifecycle", {})})
    write_json(output, gate, overwrite)
    return 0


def load_eval(path: Path) -> dict:
    value = read_json(path)
    if value.get("schema") != "fgate-eval-v1" or \
            not isinstance(value.get("harness_fingerprint"), str) or \
            not isinstance(value.get("cases"), dict) or not value["cases"]:
        raise ValueError("eval schema, harness_fingerprint and cases are required")
    for case_id, case in value["cases"].items():
        if not isinstance(case_id, str) or not isinstance(case, dict) or \
                case.get("status") not in ("PASS", "FAIL"):
            raise ValueError("eval case requires id and PASS/FAIL status")
        for name in ("severe_findings", "worker_tokens", "reviewer_tokens", "sol_tokens"):
            if not isinstance(case.get(name, 0), int) or case.get(name, 0) < 0:
                raise ValueError(f"eval case {case_id} has invalid {name}")
    return value


def compare_evals(baseline_path: Path, candidate_path: Path, output: Path,
                  overwrite: bool) -> int:
    baseline, candidate = load_eval(baseline_path), load_eval(candidate_path)
    if set(baseline["cases"]) != set(candidate["cases"]):
        raise ValueError("baseline and candidate must contain the same cases")
    regressions, improvements = [], []
    totals = {side: {name: 0 for name in ("worker_tokens", "reviewer_tokens", "sol_tokens")}
              for side in ("baseline", "candidate")}
    for case_id in sorted(baseline["cases"]):
        before, after = baseline["cases"][case_id], candidate["cases"][case_id]
        protected = before.get("protected", True)
        if protected and before["status"] == "PASS" and after["status"] != "PASS":
            regressions.append(f"{case_id}: protected PASS became FAIL")
        if after.get("severe_findings", 0) > before.get("severe_findings", 0):
            regressions.append(f"{case_id}: severe findings increased")
        if before["status"] == "FAIL" and after["status"] == "PASS":
            improvements.append(f"{case_id}: FAIL became PASS")
        for name in totals["baseline"]:
            totals["baseline"][name] += before.get(name, 0)
            totals["candidate"][name] += after.get(name, 0)
    deltas = {name: totals["candidate"][name] - totals["baseline"][name]
              for name in totals["baseline"]}
    for name, delta in deltas.items():
        if delta < 0:
            improvements.append(f"{name}: {delta}")
    status = "REJECTED" if regressions else "READY_FOR_SOL" if improvements else "NO_CHANGE"
    report = {
        "schema": "fgate-eval-comparison-v1", "status": status,
        "baseline_sha256": value_hash(baseline), "candidate_sha256": value_hash(candidate),
        "baseline_harness_fingerprint": baseline["harness_fingerprint"],
        "candidate_harness_fingerprint": candidate["harness_fingerprint"],
        "cases": len(baseline["cases"]), "regressions": regressions,
        "improvements": improvements, "token_deltas": deltas,
        "promotion_decision": None, "promotion_decision_owner": "codex-default",
    }
    write_json(output, report, overwrite)
    return 0 if status == "READY_FOR_SOL" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("project", type=Path); command.add_argument("task", type=Path)
    command.add_argument("output", type=Path); command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("run-worker")
    for name in ("project", "task", "packet", "output"):
        command.add_argument(name, type=Path)
    command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("import-worker")
    for name in ("project", "task", "packet", "handoff", "output"):
        command.add_argument(name, type=Path)
    command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("checks")
    for name in ("project", "task", "packet", "worker", "output"):
        command.add_argument(name, type=Path)
    command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("review")
    for name in ("project", "task", "packet", "worker", "checks", "output"):
        command.add_argument(name, type=Path)
    command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("finalize")
    for name in ("project", "task", "packet", "worker", "output"):
        command.add_argument(name, type=Path)
    command.add_argument("--checks", type=Path); command.add_argument("--review", type=Path)
    command.add_argument("--overwrite", action="store_true")
    command = commands.add_parser("compare")
    command.add_argument("baseline", type=Path); command.add_argument("candidate", type=Path)
    command.add_argument("output", type=Path); command.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            return prepare(args.project, args.task, args.output, args.overwrite)
        if args.command == "run-worker":
            return run_worker(args.project, args.task, args.packet, args.output, args.overwrite)
        if args.command == "import-worker":
            return import_worker(args.project, args.task, args.packet, args.handoff,
                                 args.output, args.overwrite)
        if args.command == "checks":
            return run_checks(args.project, args.task, args.packet, args.worker,
                              args.output, args.overwrite)
        if args.command == "review":
            return review(args.project, args.task, args.packet, args.worker, args.checks,
                          args.output, args.overwrite)
        if args.command == "finalize":
            return finalize(args.project, args.task, args.packet, args.worker, args.output,
                            args.checks, args.review, args.overwrite)
        return compare_evals(args.baseline, args.candidate, args.output, args.overwrite)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"fgate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
