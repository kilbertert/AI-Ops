# AI Ops Charging Diagnostics

This project is a read-only diagnostic runtime for charging order incidents. An engineer can submit a natural-language report such as:

```text
订单 2079842220423700481 金额不对，帮我排查
```

The runtime extracts the order number and intent, executes bounded queries against MySQL, TDengine, and Redis, applies rules derived from the production backend source, and returns an evidence-based report.

## Safety Contract

- No UPDATE, DELETE, INSERT, DDL, refund, recalculation, message replay, or service restart capability exists.
- MySQL queries run inside read-only transactions and use fixed parameterized SQL.
- TDengine queries always include device, time range, selected columns, and LIMIT.
- Redis inspection uses metadata and bounded reverse-range reads only.
- Credentials come only from environment variables and are always redacted from output.
- Reports omit user IDs, VINs, card numbers, plate numbers, and raw protocol payloads.

The production service account discovered during inventory is over-privileged and is not used by this runtime. The verified deployment uses dedicated MySQL, Redis, and SSH identities. TDengine Community Edition requires the strict loopback-only query proxy described in `ops/` because it cannot grant a database-level read-only role.

## Commands

Install dependencies:

```bash
uv sync --dev
```

Run an offline synthetic example:

```bash
uv run aiops diagnose "订单 TEST-YKC-0001 金额异常" --fixture examples/fixtures/ykc_amount_mismatch.json
```

Check live data-source connectivity without reading an order:

```bash
set -a
source /path/to/production.env
set +a
uv run aiops doctor
```

Diagnose a live order:

```bash
uv run aiops diagnose "订单 2079842220423700481 中途停止" --tenant-id TENANT_ID
```

Emit machine-readable output:

```bash
uv run aiops diagnose "订单 2079842220423700481 金额异常" --json
```

Start the interactive shell:

```bash
uv run aiops shell
```

## Current Rule Sources

- `SOP.md`
- `充电桩问题排查SOP.md`
- `backend-v2-domestic/cloud-charging-pile`

The backend snapshot is deliberately ignored by this repository. It remains a read-only reference and should later be replaced with a pinned source commit or submodule once the canonical remote is known.

## Known Validation Gap

There are currently no human-verified incident conclusions. Automated tests and bounded production replays verify implementation consistency, but production accuracy cannot be accepted until several real incidents and their engineer-confirmed conclusions pass end-to-end comparison. Two-wheel charging remains explicitly unsupported in the first release.
