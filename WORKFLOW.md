# Fgate project workflow

Codex default model is the main Agent and final decision owner. Keep its normal path compact:

Luna uses Lite: the main Agent dispatches it as an external subagent, imports its identity-bound
handoff, runs deterministic checks, and sends the compact packet directly to Sol. No cheap
Reviewer or multi-round local Harness is used.

DeepSeek and other local workers use Full:

1. Freeze the task, governed Harness files, context identities, Worker and Reviewer profiles.
2. Run the configured cheap Worker once. If it reports uncertainty, loops, malformed completion,
   timeout, or a need for architectural help, build the Sol assistance packet and help or take over.
3. Run all registered deterministic checks. A failure cannot be waived by either model.
4. Run the cheap Reviewer in a fresh context. Prefer a different cheap model when evidence shows
   it is better; using the same model in a new process is allowed and recorded.
5. `REWORK` sends the exact finding list to the Worker once. A second failure, disagreement,
   evidence conflict, or `ESCALATE` goes to Sol.
6. Sol reads only the compact packet and chooses `PASS`, `REWORK`, or `TAKEOVER`. High-risk tasks
   always receive deep Sol review.

Do not expose Worker chain-of-thought to the Reviewer or Sol. Preserve only task outputs,
structured findings, checks, usage, disputes, and lifecycle evidence.

Repeated findings may improve Harness files through an ordinary Git change. Prefer executable
checks over longer prompts. Replay protected historical cases, reject any known regression, and
let Sol decide promotion. Never let a live task rewrite the governed Harness.
