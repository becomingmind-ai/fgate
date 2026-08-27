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

### 让 Codex 在你的项目里使用 Fgate

从项目根目录下载本仓库到 `./fgate`，然后让 Codex 读取 `fgate/README.md` 与
`fgate/WORKFLOW.md`。普通的 Luna High 开发任务推荐 Lite：Luna 直接实现、测试和自审，
Fgate 只保留冻结范围、确定性检查和 Sol EXIT packet。

可直接把下面提示词发给 Codex：

```text
在当前项目启用本地 ./fgate 的 Luna Lite 工作流。先读取 ./fgate/README.md 和
./fgate/WORKFLOW.md；为本任务在项目内创建或更新 Fgate project/task JSON，冻结目标、
允许修改范围、hard gate、检查和 requires_weight_reload。

将 Luna High 作为 Codex 子 Agent 完成实现、测试和自审。不要使用 DeepSeek endpoint Harness。
完成后生成与 frozen packet_sha256 绑定的 fgate-lite-handoff-v1，依次运行 import-worker、
checks 和 finalize（Lite 不使用 cheap Reviewer）。把紧凑 Sol Gate packet、diff、测试结果、
风险和未决项交给主 Agent 做一次 EXIT 审核。

不自动重试；首次失败只允许一次精确返工。再次失败、证据冲突、hard gate 异常、模型卡住，或
CUDA/NCCL/ABI、四节点和高风险任务，立即交给 Sol/主 Agent 接管。
```

对本地 endpoint 或能力不稳定的模型，使用 Full：执行 `run-worker`、`checks`、`review` 和
带 `--review` 的 `finalize`。示例 `examples/dgx-project.json` 是 DGX 项目参考，不应原样复制到
其他项目；替换其中的 workspace、Worker、Reviewer、检查和 Harness 文件即可。

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

## Human quick start: use Fgate through Codex

Clone this repository into your project as `./fgate`, then ask Codex to read `fgate/README.md`
and `fgate/WORKFLOW.md`. For ordinary Luna High work, use Lite: Luna implements, tests, and
self-reviews directly; Fgate keeps only scope freezing, deterministic checks, and the Sol EXIT
packet.

Send this prompt to Codex:

```text
Enable the local ./fgate Luna Lite workflow for this project. First read ./fgate/README.md and
./fgate/WORKFLOW.md. Create or update Fgate project/task JSON for this task in the project, and
freeze the objective, allowed change scope, hard gates, checks, and requires_weight_reload.

Use Luna High as a Codex subagent for implementation, tests, and self-review. Do not use the
DeepSeek endpoint Harness. When it finishes, create an fgate-lite-handoff-v1 bound to the frozen
packet_sha256; run import-worker, checks, then finalize without a cheap Reviewer. Give the main
Agent one compact Sol Gate packet with the diff, test results, risks, and open issues for EXIT
review.

Do not retry automatically. Allow one precise rework after the first failure only. Escalate a
second failure, evidence conflict, hard-gate anomaly, model loop, or CUDA/NCCL/ABI, four-node,
or other high-risk work to Sol/the main Agent immediately.
```

For local endpoints or less reliable models, use Full: `run-worker`, `checks`, `review`, and
`finalize --review`. `examples/dgx-project.json` is a DGX reference, not a drop-in configuration:
replace its workspace, Worker, Reviewer, checks, and Harness files for your project.

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
