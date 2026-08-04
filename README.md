# AI Ops Charging Diagnostics

This project is a read-only diagnostic runtime for charging order incidents. An engineer can submit a natural-language report such as:

```text
订单 2079842220423700481 金额不对，帮我排查
```

The runtime extracts the order number and intent, executes bounded queries against MySQL, TDengine, and Redis, applies rules derived from the production backend source, and returns an evidence-based report.

The Codex-native path (`agent-diagnose`) inverts control: Codex selects the
read-only evidence tools and makes the causal judgment, while the harness keeps
the incident identity, query limits, evidence journal, redaction, confidence
caps, structured result contract, and resume state immutable. The existing
`diagnose` command remains a deterministic known-runbook fast path and advisory
comparison source.

## Safety Contract

- No UPDATE, DELETE, INSERT, DDL, refund, recalculation, message replay, or service restart capability exists.
- MySQL queries run inside read-only transactions and use fixed parameterized SQL.
- TDengine queries always include device, time range, selected columns, and LIMIT.
- Redis inspection uses metadata and bounded reverse-range reads only.
- Credentials come only from environment variables and are always redacted from output.
- Reports omit user IDs, VINs, card numbers, plate numbers, and raw protocol payloads.
- Codex runs through a pluggable Responses API provider. `AIOPS_CODEX_BASE_URL`
  is non-secret and `--key-slot` selects a private `0600` key file under
  `AIOPS_CODEX_KEY_DIR`; no API key is stored in Git or run artifacts.

The production application account discovered during inventory is over-privileged and is not used by this runtime. The verified deployment uses dedicated MySQL, Redis, and SSH identities. Changing the application account requires a separate dependency audit and credential rotation; this diagnostic project must never inherit it as a shortcut. TDengine Community Edition requires the strict loopback-only query proxy described in `ops/` because it cannot grant a database-level read-only role.

## Commands

Install dependencies:

```bash
uv sync --dev
```

Initialize platform-native private paths and install a pluggable provider key:

```bash
uv run aiops init
uv run aiops key-install primary
uv run aiops agent-doctor --key-slot primary
```

Use `aiops --config /path/to/production.env ...` to select another private
configuration file. `aiops paths` prints the resolved non-secret locations.

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
uv run aiops diagnose "订单 2079842220423700481 金额异常" --tenant-id TENANT_ID --json
```

Run the Codex-native diagnostic agent against a synthetic case:

```bash
uv run aiops agent-diagnose "订单 TEST-YKC-0001 金额异常" \
  --fixture examples/fixtures/ykc_amount_mismatch.json \
  --key-slot default --json
```

Resume the same incident/thread after an interruption, or switch to another
key for the same provider endpoint:

```bash
uv run aiops agent-resume RUN_ID --key-slot backup --json
```

Resume compares the configured `AIOPS_CODEX_BASE_URL` with the endpoint
recorded for the run and refuses to redirect a stored key to another host. A
fixture is copied into the private run workspace and hash-checked on resume;
the model sandbox cannot read the fixture or harness control files.

Start the interactive shell:

```bash
uv run aiops shell
```

## Portable Windows And Linux Bundle

The portable build contains the Python application, staged diagnostic
references, synthetic fixtures, and the platform-specific native Codex runtime
pinned by the Python SDK. It does not contain API keys or production database
credentials.

Build on the target operating system:

```bash
uv sync --locked --dev
uv run python packaging/build_portable.py
```

The command creates `dist/aiops/` and a versioned zip, extracts that zip outside
the source tree, then smoke-tests the packaged executable with a minimal PATH,
an isolated private home, the bundled Codex runtime, and all three offline
fixtures.
Windows 11 is the recommended deployment baseline; see
[`docs/portable.md`](docs/portable.md) for OpenSSH, sandbox, ACL, and first-run
requirements.

## Current Rule Sources

- `SOP.md`
- `充电桩问题排查SOP.md`
- `backend-v2-domestic/cloud-charging-pile`

The backend snapshot is deliberately ignored by this repository. It remains a read-only reference and should later be replaced with a pinned source commit or submodule once the canonical remote is known.

## Known Validation Gap

There are currently no human-verified incident conclusions. Automated tests and bounded production replays verify implementation consistency, but production accuracy cannot be accepted until several real incidents and their engineer-confirmed conclusions pass end-to-end comparison. Two-wheel charging remains explicitly unsupported in the first release.

The Codex provider is configured through the OpenAI-compatible Responses API
settings in `.env.example`. A provider can reject requests because of quota or
model policy even when the harness is healthy; such failures remain in the run
event journal and are resumable without changing the incident manifest.
