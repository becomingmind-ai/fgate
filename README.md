# Fgate

[中文](#中文说明) | [English](#english)

## 中文说明

Fgate 是一个可下载到项目本地的轻量工程证据与模型协作组件。它让便宜模型承担绝大多数实现
和审核工作，同时由 Codex 默认模型（Sol）保留最终决断权。

```text
便宜 Worker → 确定性检查 → 便宜 Reviewer → 紧凑 Sol 决策包
```

Reviewer 使用独立进程和全新上下文。它只能建议通过、要求一次返工或升级处理，不能作出最终
PASS。Worker 阻塞、循环、输出异常，或者出现高风险任务、身份冲突和证据异常时，Fgate 会将
工作交给 Sol 深度审核或接管，不会隐藏失败或自动无限重试。

Fgate 核心只使用 Python 标准库，不依赖 daemon、模型 SDK、向量数据库或训练框架。Worker 和
Reviewer 都通过包含 `{input}` 与 `{output}` 的 argv profile 配置，因此可以连接 DeepSeek、
Luna、GLM、Kimi 或其他本地/低成本模型命令。

对于 Luna 等 Codex 原生子 Agent，可将 Worker profile 设置为 `provider=codex-subagent`、
`workflow=lite`。此时 Codex 直接编排 Worker，Fgate 使用 `import-worker` 导入带 packet SHA 的
结构化 handoff，然后执行确定性检查并生成 Sol EXIT packet；不会调用本地模型 Harness 或
Cheap Reviewer。

### 基本命令

```bash
python3 fgate/fgate.py prepare PROJECT TASK PACKET
python3 fgate/fgate.py run-worker PROJECT TASK PACKET WORKER_RESULT
python3 fgate/fgate.py import-worker PROJECT TASK PACKET HANDOFF WORKER_RESULT
python3 fgate/fgate.py checks PROJECT TASK PACKET WORKER_RESULT CHECKS
python3 fgate/fgate.py review PROJECT TASK PACKET WORKER_RESULT CHECKS REVIEW
python3 fgate/fgate.py finalize PROJECT TASK PACKET WORKER_RESULT SOL_PACKET \
  --checks CHECKS --review REVIEW
python3 fgate/fgate.py compare BASELINE_EVAL CANDIDATE_EVAL COMPARISON
```

如果 Worker 需要帮助，可以省略检查和 Reviewer，直接生成 Sol 协助包：

```bash
python3 fgate/fgate.py finalize PROJECT TASK PACKET WORKER_RESULT SOL_PACKET
```

### Harness 进化

Harness 是受 SHA 指纹和 Git 保护的普通项目文件。候选改进必须重放相关历史案例；任何受保护
案例由 PASS 变成 FAIL，或严重问题增加，都会被拒绝。没有已知退化且确有修复或 token 降低时，
候选只会进入 `READY_FOR_SOL`，仍需 Sol 批准才能晋升。Fgate 不替代 Git 的版本、diff 和回滚。

### 验证

```bash
python3 fgate/test_fgate.py
python3 -m py_compile fgate/fgate.py fgate/test_fgate.py \
  scripts/models/deepseek-v4-agent-harness.py
```

`examples/dgx-project.json` 展示了 DGX 项目的配置方式。它复用项目已有的只读 DeepSeek harness，
不会操作四节点集群或改变模型权重和 R6 Gate；其他项目应替换成自己的 Worker、Reviewer、检查
命令和 Harness 文件。

---

## English

Fgate is a project-local workflow that gives almost all implementation and review work to
cheap models while keeping the Codex default model as the final arbiter.

```text
Full: cheap Worker -> deterministic checks -> cheap Reviewer -> compact Sol packet
Lite: Luna subagent -> deterministic checks -> compact Sol packet
```

The Reviewer always runs in a fresh process and context. It may recommend `PASS`, request one
rework, or escalate uncertainty. Only Codex/Sol can make the final decision. High-risk tasks,
identity conflicts, malformed evidence, loops, and blocked Workers are routed to deeper Sol
help instead of being hidden or retried automatically.

Fgate uses only the Python standard library. It has no daemon, model SDK, vector database,
training framework, or provider-specific dependency.

For a Codex-native subagent such as Luna, set the Worker profile to
`provider=codex-subagent` and `workflow=lite`. Codex orchestrates the Worker directly;
`import-worker` imports its packet-bound structured handoff, deterministic checks run normally,
and Fgate creates the Sol EXIT packet without invoking a local-model harness or cheap Reviewer.

## Model contract

Worker and Reviewer profiles are ordinary argv arrays containing `{input}` and `{output}`.
The command reads one JSON packet and exclusively creates one JSON result. This supports any
local CLI or harness, including DeepSeek, Luna, GLM, and Kimi, without adding provider code to
Fgate.

Worker result:

```json
{
  "status": "complete",
  "finish_reason": "stop",
  "answer": "result, patch, or evidence",
  "changes": [],
  "self_review": [],
  "usage": {}
}
```

A blocked or looping Worker returns `status=needs_help` and a `help_request`. Fgate then builds
a Sol assistance packet without pretending the task passed.

Reviewer result, either directly or as the exact JSON string in `answer`:

```json
{
  "decision": "RECOMMEND_PASS",
  "findings": [],
  "uncertainties": [],
  "risk": "low"
}
```

## Commands

```bash
python3 fgate/fgate.py prepare PROJECT TASK PACKET
python3 fgate/fgate.py run-worker PROJECT TASK PACKET WORKER_RESULT
python3 fgate/fgate.py import-worker PROJECT TASK PACKET LITE_HANDOFF WORKER_RESULT
python3 fgate/fgate.py checks PROJECT TASK PACKET WORKER_RESULT CHECKS
python3 fgate/fgate.py review PROJECT TASK PACKET WORKER_RESULT CHECKS REVIEW
python3 fgate/fgate.py finalize PROJECT TASK PACKET WORKER_RESULT SOL_PACKET \
  --checks CHECKS --review REVIEW
python3 fgate/fgate.py compare BASELINE_EVAL CANDIDATE_EVAL COMPARISON
```

For Luna Lite, set the active Worker profile to `provider=codex-subagent`,
`model_id=gpt-5.6-luna`, and `workflow=lite`. The main Agent runs Luna externally and imports a
`fgate-lite-handoff-v1` carrying the frozen `packet_sha256`; `finalize` then requires checks but
not `--review`. Full local-model profiles continue to use `run-worker`, `review`, and `--review`.

If the Worker needs help, omit checks and review:

```bash
python3 fgate/fgate.py finalize PROJECT TASK PACKET WORKER_RESULT SOL_PACKET
```

Outputs are exclusive by default. `--overwrite` is intended for disposable local smoke files,
not immutable Gate evidence.

## Harness evolution

Harness files are normal project files protected by SHA fingerprints and Git. Repeated Reviewer
findings may produce a small Harness change, but the active Harness is never modified by the
Worker during a task. A change is promoted only after relevant historical cases and deterministic
checks show no known regression and Sol approves the compact comparison. Git provides versioning,
diff, rollback, and promotion; Fgate does not duplicate those systems.

`compare` provides the one mechanical promotion rule. Both eval files use
`schema=fgate-eval-v1`, a `harness_fingerprint`, and the same case map. Each case records
`status`, optional `protected` (default true), `severe_findings`, and Worker/Reviewer/Sol token
counts. A protected PASS becoming FAIL or any increase in severe findings is `REJECTED`.
No known regression plus at least one repaired case or token reduction is `READY_FOR_SOL`;
only Sol may fill the promotion decision.

## DGX validation

`examples/dgx-project.json` reuses the existing read-only DeepSeek harness for both roles. The
second invocation has a fresh context and is recorded as `same_model_fresh_context`. Replace the
active profile or command to test another cheap model. The smoke task is offline and declares
`requires_weight_reload=false`; it does not operate the four-node cluster or change the R6 Gate.

Run deterministic tests:

```bash
python3 fgate/test_fgate.py
python3 -m py_compile fgate/fgate.py fgate/test_fgate.py \
  scripts/models/deepseek-v4-agent-harness.py
```
