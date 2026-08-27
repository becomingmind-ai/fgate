#!/usr/bin/env python3
"""Standard-library end-to-end tests for reusable Fgate."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FGATE = ROOT / "fgate.py"


class FgateTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="fgate-v2-test-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        (self.work / "HARNESS.md").write_text("Stay in scope.\n")
        (self.work / "context.txt").write_text("password=do-not-leak\nold\n")
        self.role = self.work / "fake_role.py"
        self.role.write_text(
            """import json, pathlib, sys
source, output, role = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
mode = sys.argv[4] if len(sys.argv) > 4 else 'pass'
packet = json.loads(source.read_text())
if role == 'worker':
    if mode == 'blocked':
        value = {'status': 'needs_help', 'finish_reason': 'stop',
                 'answer': 'uncertain', 'help_request': 'need architecture guidance'}
    else:
        if mode == 'touch-harness':
            pathlib.Path('HARNESS.md').write_text('bad mutation\\n')
        if mode == 'touch-context':
            pathlib.Path('context.txt').write_text('bad mutation\\n')
        value = {'status': 'complete', 'finish_reason': 'stop',
                 'answer': 'password=hidden candidate', 'changes': ['context.txt'],
                 'self_review': ['scope checked'], 'usage': {'completion_tokens': 7}}
else:
    if mode == 'malformed':
        answer = 'not json'
    else:
        decision = {'pass': 'RECOMMEND_PASS', 'rework': 'REWORK',
                    'escalate': 'ESCALATE'}[mode]
        answer = json.dumps({'decision': decision,
                             'findings': [] if decision == 'RECOMMEND_PASS' else ['finding'],
                             'uncertainties': [] if decision != 'ESCALATE' else ['uncertain'],
                             'risk': packet['risk']})
    value = {'status': 'complete', 'finish_reason': 'stop', 'answer': answer,
             'usage': {'completion_tokens': 5}}
output.write_text(json.dumps(value))
""")
        self.project = self.work / "project.json"
        self.task = self.work / "task.json"
        self.write_project()
        self.write_task()

    def write_project(self, worker_mode="pass", reviewer_mode="pass"):
        value = {
            "schema": "fgate-project-v1", "project": "test", "workspace": ".",
            "supervisor": "codex-default", "active_worker": "cheap-worker",
            "workers": {"cheap-worker": {"command": [
                sys.executable, str(self.role), "{input}", "{output}", "worker", worker_mode]}},
            "active_reviewer": "cheap-reviewer",
            "reviewers": {"cheap-reviewer": {"command": [
                sys.executable, str(self.role), "{input}", "{output}", "reviewer",
                reviewer_mode]}},
            "checks": {"pass": [sys.executable, "-c", "print('ok')"]},
            "harness": ["HARNESS.md"],
            "policy": {"automatic_retry": False, "max_rework": 1,
                       "codex_gate_required": True},
            "requires_weight_reload": False,
        }
        self.project.write_text(json.dumps(value))

    def write_task(self, risk="low", context=None):
        value = {
            "schema": "fgate-task-v1", "id": "task-1", "objective": "test task",
            "hard_gates": ["checks pass"],
            "context": context if context is not None else ["context.txt"],
            "checks": ["pass"], "risk": risk, "worker_mode": "read_only",
            "requires_weight_reload": False, "real_experiment": {"status": "not_run"},
            "lifecycle": {"automatic_retry": False},
        }
        self.task.write_text(json.dumps(value))

    def write_lite_project(self):
        value = json.loads(self.project.read_text())
        value["active_worker"] = "luna-high"
        value["workers"]["luna-high"] = {
            "provider": "codex-subagent", "model_id": "gpt-5.6-luna",
            "workflow": "lite"}
        self.project.write_text(json.dumps(value))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(FGATE), *map(str, args)], cwd=self.work,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False)

    def paths(self):
        return tuple(self.work / name for name in
                     ("packet.json", "worker.json", "checks.json", "review.json", "sol.json"))

    def complete_to_review(self):
        packet, worker, checks, review, sol = self.paths()
        self.assertEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        self.assertEqual(self.cli("run-worker", self.project, self.task, packet,
                                  worker).returncode, 0)
        self.assertEqual(self.cli("checks", self.project, self.task, packet, worker,
                                  checks).returncode, 0)
        result = self.cli("review", self.project, self.task, packet, worker, checks, review)
        return packet, worker, checks, review, sol, result

    def test_happy_path_is_compact_codex_decision(self):
        packet, worker, checks, review, sol, result = self.complete_to_review()
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads(packet.read_text())
        self.assertNotIn("do-not-leak", json.dumps(prepared))
        self.assertEqual(json.loads(worker.read_text())["answer"],
                         "password=[REDACTED] candidate")
        final = self.cli("finalize", self.project, self.task, packet, worker, sol,
                         "--checks", checks, "--review", review)
        self.assertEqual(final.returncode, 0, final.stderr)
        gate = json.loads(sol.read_text())
        self.assertEqual(gate["status"], "ready_for_sol")
        self.assertEqual(gate["recommended_action"], "DECIDE")
        self.assertEqual(gate["cheap_review"]["decision"], "RECOMMEND_PASS")
        self.assertIsNone(gate["sol_decision"])
        self.assertEqual(gate["sol_decision_owner"], "codex-default")

    def test_reviewer_rework_returns_to_worker(self):
        self.write_project(reviewer_mode="rework")
        packet, worker, checks, review, sol, result = self.complete_to_review()
        self.assertEqual(result.returncode, 1)
        final = self.cli("finalize", self.project, self.task, packet, worker, sol,
                         "--checks", checks, "--review", review)
        self.assertEqual(final.returncode, 0, final.stderr)
        gate = json.loads(sol.read_text())
        self.assertEqual((gate["status"], gate["recommended_action"]),
                         ("return_to_worker", "REWORK_ONCE"))

    def test_malformed_reviewer_escalates(self):
        self.write_project(reviewer_mode="malformed")
        packet, worker, checks, review, sol, result = self.complete_to_review()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(review.read_text())["decision"], "ESCALATE")
        final = self.cli("finalize", self.project, self.task, packet, worker, sol,
                         "--checks", checks, "--review", review)
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(json.loads(sol.read_text())["recommended_action"],
                         "ASSIST_OR_TAKEOVER")

    def test_blocked_worker_routes_directly_to_sol(self):
        self.write_project(worker_mode="blocked")
        packet, worker, _, _, sol = self.paths()
        self.assertEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        result = self.cli("run-worker", self.project, self.task, packet, worker)
        self.assertEqual(result.returncode, 1)
        final = self.cli("finalize", self.project, self.task, packet, worker, sol)
        self.assertEqual(final.returncode, 0, final.stderr)
        gate = json.loads(sol.read_text())
        self.assertEqual(gate["status"], "needs_sol_assistance")
        self.assertEqual(gate["recommended_action"], "ASSIST_OR_TAKEOVER")
        self.assertIn("architecture guidance", gate["worker"]["help_request"])

    def test_high_risk_never_gets_decision_only(self):
        self.write_task(risk="high")
        packet, worker, checks, review, sol, result = self.complete_to_review()
        self.assertEqual(result.returncode, 0)
        final = self.cli("finalize", self.project, self.task, packet, worker, sol,
                         "--checks", checks, "--review", review)
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(json.loads(sol.read_text())["recommended_action"], "DEEP_REVIEW")

    def test_luna_lite_imports_external_handoff_and_skips_reviewer(self):
        self.write_lite_project()
        packet, worker, checks, review, sol = self.paths()
        handoff = self.work / "handoff.json"
        self.assertEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        self.assertNotEqual(self.cli("run-worker", self.project, self.task, packet,
                                     worker).returncode, 0)
        frozen = json.loads(packet.read_text())
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        handoff.write_text(json.dumps({
            "schema": "fgate-lite-handoff-v1", "packet_sha256": digest,
            "status": "complete", "finish_reason": "stop", "answer": "done",
            "changes": [], "self_review": ["scope checked"], "usage": {}}))
        self.assertEqual(self.cli("import-worker", self.project, self.task, packet,
                                  handoff, worker).returncode, 0)
        self.assertEqual(self.cli("checks", self.project, self.task, packet, worker,
                                  checks).returncode, 0)
        final = self.cli("finalize", self.project, self.task, packet, worker, sol,
                         "--checks", checks)
        self.assertEqual(final.returncode, 0, final.stderr)
        gate = json.loads(sol.read_text())
        self.assertEqual((gate["status"], gate["recommended_action"]),
                         ("ready_for_sol", "DECIDE"))
        self.assertEqual(gate["cheap_review"]["status"], "skipped")
        handoff.write_text(json.dumps({"schema": "fgate-lite-handoff-v1",
                                       "packet_sha256": "tampered"}))
        self.assertNotEqual(self.cli("import-worker", self.project, self.task, packet,
                                     handoff, review).returncode, 0)

    def test_boundaries_fail_closed(self):
        self.write_task(context=["../outside"])
        packet = self.work / "bad.json"
        self.assertNotEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        self.write_task()
        self.write_project(worker_mode="touch-harness")
        self.assertEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        worker = self.work / "worker.json"
        self.assertNotEqual(self.cli("run-worker", self.project, self.task, packet,
                                     worker).returncode, 0)

    def test_read_only_worker_cannot_change_context(self):
        self.write_project(worker_mode="touch-context")
        packet, worker, _, _, _ = self.paths()
        self.assertEqual(self.cli("prepare", self.project, self.task, packet).returncode, 0)
        self.assertNotEqual(self.cli("run-worker", self.project, self.task, packet,
                                     worker).returncode, 0)

    def test_core_uses_only_standard_library(self):
        tree = ast.parse(FGATE.read_text())
        modules = {node.names[0].name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import)}
        modules |= {node.module.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module}
        self.assertLessEqual(modules, sys.stdlib_module_names)

    def test_eval_compare_rejects_regression_and_accepts_improvement(self):
        baseline, candidate, report = (self.work / "baseline.json",
                                       self.work / "candidate.json",
                                       self.work / "comparison.json")
        base = {"schema": "fgate-eval-v1", "harness_fingerprint": "old", "cases": {
            "protected": {"status": "PASS", "severe_findings": 0, "sol_tokens": 10},
            "target": {"status": "FAIL", "severe_findings": 1, "sol_tokens": 10}}}
        baseline.write_text(json.dumps(base))
        bad = json.loads(json.dumps(base))
        bad["harness_fingerprint"] = "bad"
        bad["cases"]["protected"]["status"] = "FAIL"
        candidate.write_text(json.dumps(bad))
        self.assertEqual(self.cli("compare", baseline, candidate, report).returncode, 1)
        self.assertEqual(json.loads(report.read_text())["status"], "REJECTED")
        good = json.loads(json.dumps(base))
        good["harness_fingerprint"] = "good"
        good["cases"]["target"].update({"status": "PASS", "severe_findings": 0,
                                         "sol_tokens": 5})
        candidate.write_text(json.dumps(good))
        self.assertEqual(self.cli("compare", baseline, candidate, report,
                                  "--overwrite").returncode, 0)
        result = json.loads(report.read_text())
        self.assertEqual(result["status"], "READY_FOR_SOL")
        self.assertEqual(result["promotion_decision_owner"], "codex-default")


if __name__ == "__main__":
    unittest.main()
