# AI-Ops gateway feature map

One file per user-facing feature. Each answers: what it is, how to reach it, how
to drive it, and what observable end state proves it works.

The map is the maintained verification source. **A proof that drives one
convenient entry point is incomplete when this index lists others.**

## Canonical navigation

The gateway is an HTTP service, not a UI. Every path starts the same way:

```bash
T=$(mktemp -d)
export AIOPS_HOME="$T" AIOPS_CONFIG_HOME="$T/config" AIOPS_DATA_HOME="$T/data"
uv run aiops init          # REQUIRED — without it `serve` dies with
                           # "gateway server production.env does not exist"
uv run aiops-gateway serve # -> http://127.0.0.1:8787
```

Then mint a credential — nothing authenticated works without one:

```bash
uv run aiops-gateway issue-enrollment --workspace ops    # -> enr_…
curl -s -X POST http://127.0.0.1:8787/v1/enroll \
  -H 'Content-Type: application/json' \
  -d '{"code":"enr_…","device_name":"verify","platform":"linux"}'   # -> aops_…
```

**Read the helper first.** `scripts/observe.py` performs exactly this sequence on
a throwaway root; prefer running it over hand-rolling the steps.

## Start here, not with a feature

**A device token is not an admin token.** A locally enrolled device reaches the
run routes (200) but is refused by the agent routes (401), because those need a
role from UPMS that local enrollment never grants. Decide which class of route
you are verifying before you read a 401 as a defect.

| Feature | Surface | Reaches it by |
|---|---|---|
| [enrollment](enrollment.md) | 设备注册 | `issue-enrollment` → `POST /v1/enroll` |
| [run-lifecycle](run-lifecycle.md) | 只读诊断 run | `POST /v1/runs`（需设备令牌） |
| [auth-boundary](auth-boundary.md) | 认证与角色边界 | 无令牌 401 / 设备令牌 200 / 管理路由 401 |

## Not on this map — needs remote services

These are real gateway features, but **not provable on this host**. If a change
touches them, say so; do not report a local pass as covering them.

- **`/diag/*` 只读诊断** — reads MySQL / TDengine / Redis on the fleet host
- **模型驱动的 run 执行** — a Codex session needs a provider key and network
- **KB 活性校验** — `AIOPS_GATEWAY_KB_SERVICE_BASE_URL` → `kb-service`
- **`admin reconcile`** — converges a real gateway store from
  `ops/environments/<env>.toml`（当前仅 `env-41.toml`）

The gateway is a **control plane**: the diagnosis runs elsewhere. A green local
run proves the API contract and the auth model, not that a diagnosis works.
