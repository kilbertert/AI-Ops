# AI-Ops — Handoff Brief

_Compact state + decisions for a Claude Code session driving the remaining AI-Ops tickets.
Created 2026-08-27._

---

## 1. Current system state

- **AFK workflow fully wired** in all 4 repos (Auto-Test / genesis-evidence /
  health-flow / AI-Ops): `.sandcastle` runner + planner (`pnpm ralph`) +
  label Actions + `CONTEXT.md` + `CODING_STANDARDS.md` + `docs/afk-workflow.md`.
- **Plan A (no auto-claim) active** — no label auto-runs an issue; driven by
  `pnpm ralph` (planner), `pnpm afk` (single), or explicit `workflow_dispatch`.
- **Providers**: `claude-ark` (GLM), `psydo` (GPT), `aliyun-deepseek` (DeepSeek).
  Only **aliyun-deepseek** is currently healthy in full sessions (ark/psydo had
  quota/429). aliyun is **slow-timeout-prone** on long multi-turn sessions.
- **Self-hosted runners online** for all repos; `AGENT_PAT` set.
- **AFK_PROFILE for AI-Ops = aliyun-deepseek**.

## 2. Ten-ticket relationship graph

Parent PRD: **#23** (充电业务诊断查询接口 / AI-Ops 直查库正式化)

```
T1   #34 /diag/order       [DONE — merged]   ╎ root, no deps
T7   #35 tsdata /tdadmin   [BLOCKED]         ╎ root, no deps — EXTERNAL repo
T9   #36 HttpSources       [DONE — merged]   ╎ root, no deps
T2   #37 /diag/comm-message                   ╎ needs #34
T3   #38 /diag/gun-property                   ╎ needs #34
T4   #39 /diag/occupy-order                   ╎ needs #34
T5   #40 /diag/redis-stream                   ╎ needs #34
T6   #41 /diag/device                         ╎ needs #34
T8   #42 内部令牌密钥配置化                      ╎ needs #34 + #35
T10  #43 收口(切流量+删凭据)                    ╎ needs #34, #37-41, #36, #42
```

## 3. Block Matrix

| Ticket | #34 | #35 | #36 | #37-41 | #42 | #43 |
|---|---|---|---|---|---|---|
| T2-T6 (#37-41) | **X** | | | | | |
| T8 (#42) | **X** | **X** | | | | |
| T10 (#43) | **X** | | **X** | **X** | **X** | — |

- #34 & #36 done → T2-T6 unblocked (still need Java / external reasoning).
- Everything funnels into #43 (the closing ticket).

## 4. First priority decisions (block everything else)

1. **T7 #35 — the fork in the road.** It needs the real `tsdata`/Java repo
   (or an explicit decision to do a Python-proto under this repo). This blocks
   T8, and T10's "删三库凭据".
   - Options: provide tsdata repo/creds → implement there; OR authorize a
     Python-prototype implementation in this repo (needs product/arch OK).
2. **T8 #42** — key rotation/config choices + whether to touch shared secrets
   (a security-sensitive decision; human in loop).
3. **T10 #43** — which traffic to cut + which credentials to delete (final,
   irreversible).

## 5. What to tell Claude Code in AI-Ops

- Read `CONTEXT.md`, `CODING_STANDARDS.md`, `docs/diag-query-api-plan.md`,
  `docs/afk-workflow.md` first.
- The AFK machinery is repo-local and ready: `.sandcastle` runner + planner +
  actions; use `pnpm afk -- <issue>` or `pnpm ralph` (planner) when an issue is
  a clean, auto-drivable AFK task. Reserve human-in-the-loop for the security /
  cross-repo / withdrawal decisions (T7 access, T8 secrets, T10 cutoff).
- Current model on this repo is `aliyun-deepseek`; it's slow on long sessions —
  expect patience or use a fast path for small tasks.
- **Do not** rely on `agent:implement` auto-triggering (Plan A — dispatch only).
- The `.gitignore` now ignores `pnpm-lock.yaml`/`pnpm-workspace.yaml` (repo
  uses npm) and `*.jsonl`/`codex-agent.state.json`.

---

_Next: after the T7/T8/T10 decisions, re-run `pnpm ralph` to drive the newly
unblocked sub-issues in dependency order; the planner already proved the
dependency-graph → parallel → merge → close loop on #34/#36._
## Execution paths (sanity for any agent reading this)

- **Planner (`pnpm ralph`)** uses the upstream-identical docker-worktree
  sandbox (`createSandbox` + `docker()` + `close()`); per-issue worktree is
  auto-cleaned. `.sandcastle/worktrees/` is gitignored.
- **Label-Action implement/review** runs on the self-hosted runner's **persistent
  workspace** (`git checkout -b` + a `docker()` container for isolation/profile).
  This is NOT a per-issue worktree; `agent/*` branches persist between runs and
  can accumulate (upstream has the same property on self-hosted). Clean them up
  explicitly — the stale-runner checkout now `reset --hard` + `clean` first.
- Do **not** read the label-Action docker container as "we diverged from the
  reference" — the reference's label-Action uses `noSandbox`; ours uses
  `docker()` as a deliberate isolation/profile-injection enhancement, and both
  run on a persistent runner workspace.
