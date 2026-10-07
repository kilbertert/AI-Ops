---
name: verify-aiops-gateway
description: "Run the AI-Ops gateway as a real service on an isolated data root and drive it over HTTP — enroll a device, call the authenticated run routes, capture request/response evidence. Use when you need to prove gateway API, enrollment, run, or auth behavior on AI-Ops rather than assume it works from unit tests."
---

# Verify the AI-Ops gateway

Bring up the **real** `aiops-gateway` against a throwaway data root and drive it
over HTTP. This skill wraps machinery the repo already has (`aiops init`,
`aiops-gateway issue-enrollment`, `aiops-gateway serve`) rather than inventing a
harness.

Grounded against commit `e1d71fc` on 2026-10-06.

> **This gateway serves real operators.** Everything below runs on a temp data
> root created for the run. Never point a verification at the real
> `AIOPS_DATA_HOME`, and never leave a gateway listening after you finish.

## What this can and cannot prove locally

| Provable here | Not provable here |
|---|---|
| gateway boots and binds a port | **diagnose against live data** — `/diag/*` needs MySQL/TDengine/Redis on the fleet host |
| `/health` and its contract fields | **model-backed runs** — a Codex session needs a provider key |
| enrollment: code → device token | **KB liveness checks** — needs `kb-service` |
| auth: which routes accept a device token | agent publish/reconcile — needs an admin role from UPMS |

The gateway is a **control plane**; the diagnosis itself runs elsewhere. A green
run here proves the API contract and the auth model, not that a diagnosis works.

## Launch

```bash
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --help
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py
```

The helper does the whole sequence on a temp root. Manual equivalent:

```bash
T=$(mktemp -d)
export AIOPS_HOME="$T" AIOPS_CONFIG_HOME="$T/config" AIOPS_DATA_HOME="$T/data"

uv run aiops init                       # ← REQUIRED FIRST; creates config/production.env
uv run aiops-gateway serve              # binds 127.0.0.1:8787
```

**`aiops init` is not optional.** Without it `serve` dies with
`ValueError: gateway server production.env does not exist` — and that message
looks like a broken checkout rather than a missing setup step.

Default port is **8787** (`/health` → 200). It is not in
`DEVELOPMENT-PORT-REGISTRY.md` because it is a per-run default on a temp root;
never expose it beyond loopback.

## Doctor

```bash
curl -s http://127.0.0.1:8787/health
# {"ok":true,"service":"aiops-gateway","version":"0.1.0","api_version":"v1",
#  "platform":"linux","business_mutations":"disabled"}
```

If `business_mutations` is not `"disabled"`, **stop** — that is a safety
invariant of this service, not a configuration detail.

Then prove auth is live, not just that the port answers:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/v1/runs   # 401 expected
```

An unauthenticated 401 is the correct answer. A 200 there would mean the auth
gate is not wired.

## Enroll a device

The documented local flow, and the only way to get a usable token:

```bash
# 1. mint a code (server-side admin operation)
uv run aiops-gateway issue-enrollment --workspace ops
# {"enrollment_code":"enr_…","workspace_id":"ops","tenant_id":null,"expires_in_seconds":600}

# 2. exchange it for a device token
curl -s -X POST http://127.0.0.1:8787/v1/enroll \
  -H 'Content-Type: application/json' \
  -d '{"code":"enr_…","device_name":"verify","platform":"linux"}'
# {"device_id":"dev_…","workspace_id":"ops","tenant_id":null,"token":"aops_…"}
```

**Three things that will bite you:**

1. **Enrollment codes are single-use.** A second `POST /v1/enroll` with the same
   code fails. Mint a fresh code for every enrollment you need.
2. **All three fields are required and `extra="forbid"`.** Omitting
   `device_name` or `platform` returns
   `INVALID_REQUEST: request validation failed` — with no field named.
3. **A device token is not an admin token.** With a valid device token,
   `/v1/runs` returns 200 but `/v1/agents` still returns 401: agent routes need a
   role from UPMS that a locally enrolled device does not have. Do not report that
   401 as a defect without checking which role the route requires.

## Drive

```bash
TOKEN=aops_…
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/v1/runs
```

Response evidence goes to `var/verify-evidence/`. **This repo does not gitignore
`var/`** (unlike health-flow and genesis-evidence), so a stray `git add -A` will
commit the evidence. Delete it before committing, or put the artifact you want to
keep in the PR body instead of the tree.

Proof standards:

- Exercise the **real HTTP surface**, not the handler function. A `TestClient`
  call is a unit test.
- Capture the **request and the response**, and the status code — for auth work
  the failure path is the evidence.
- Note which routes you exercised and which you could not reach **with the token
  you had**, rather than implying the whole API was covered.

## Cleanup

- Stop the gateway you started. **Never kill by process name** — `aiops-gateway`
  may also be a systemd unit on this host.
- Remove the temp data root. Evidence lives outside it and must survive.
- Confirm the port is released before you finish.

## Helpers

| Helper | Invocation | Purpose |
|---|---|---|
| `scripts/observe.py` | `uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py` | temp root → `init` → `serve` → doctor → enroll → evidence → teardown |

## Known limits

- **`/diag/*`, model-backed runs, and KB liveness need remote services.** If a
  change touches those paths, this skill cannot prove it — say so explicitly
  instead of reporting a pass.
- The gateway is read-only by design (`business_mutations: disabled`). A change
  that makes it write business state should be treated as a contract breach, not
  a verified feature.
- `ops/environments/` currently declares only `env-41.toml`. `admin reconcile`
  converges a real gateway store; running it locally proves the code path but not
  the production inventory.
- **本清单会腐烂。** 改动让某个 feature 文件失真时（路由搬了、控件改名了、
  某一步不再可用），**在同一次改动里更新那个文件**。
  需要定期通检整个清单时，运行 `maintain-verification-skill` —— 它来自
  `pstack-verify` 插件（本仓不包含它；用 `claude plugin details pstack-verify`
  确认是否已装，或 `claude plugin install pstack-verify@pstack-verify` 安装）。
  不要让陈旧条目留着：**过期的清单会报一个它没挣到的通过**。
