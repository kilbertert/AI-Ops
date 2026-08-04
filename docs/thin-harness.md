# Codex-Native Thin Harness

## Control Inversion

`aiops diagnose` is still available as a deterministic known-runbook fast path.
`aiops agent-diagnose` uses a persistent Codex thread as the diagnostic actor:

```text
engineer feedback
       |
       v
immutable incident manifest -> Codex thread
                                  |  tool_requests
                                  v
                         bounded read-only adapters
                                  |
                                  v
                         private evidence journal
                                  |
                                  +---- resume same thread
                                  v
                         structured diagnosis
                                  |
                         contract validator
```

Codex chooses which evidence to request and how to combine the results. The
harness does not select a causal winner. It only makes the requested operation
legal, bounded, tenant/order scoped, journaled, sanitized, and resumable.

## Harness Invariants

- `IncidentManifest` fixes the order number, tenant, feedback intent, and source hash.
- `AgentWorkspace` is private (`0700`) and contains staged SOP/backend references, no production credentials, and no raw cross-run state.
- Synthetic fixtures are copied into a hidden, hash-checked input path so a resumed run cannot silently read changed test data.
- `EvidenceJournal` stores one immutable artifact per tool result, its SHA-256, bounded request metadata, and source status; dependent reads re-check the hash.
- `DiagnosticToolExecutor` exposes only order, fee, device, TDengine, Redis, and advisory known-runbook tools. There is no SQL input, arbitrary table selection, shell-to-production path, or mutation tool.
- `AgentResultValidator` rejects identity drift, missing evidence references, known-runbook-only conclusions, high confidence after source failure or unresolved dependencies, secret leakage, and claims that a forbidden business action was executed. It does not rewrite the model's root cause.
- A provider failure, timeout, malformed response, or interrupted process leaves the same `thread_id` and `run_id` resumable.

## Provider And Key Slots

The provider is configured in a generated private Codex home using:

- `AIOPS_CODEX_BASE_URL`: the non-secret Responses API origin.
- `AIOPS_CODEX_API_KEY`: an ephemeral environment override, if present.
- `AIOPS_CODEX_API_KEY_FILE`: an explicit private key file, if configured.
- `AIOPS_CODEX_KEY_DIR/<slot>.key`: the default pluggable slot store.
- `--key-slot`: select a different key for the same base URL without changing code.

Key files must be owned by the `claude` account and mode `0600`. The key is
passed only to the Codex app-server process as an internal provider variable;
model-generated shell commands receive a filtered environment that excludes
secret variables. Runtime config disables analytics and OTel exporters, and
denies model reads of run state, event, result, journal, and fixture-control
files. The result and journal record only the slot name and, in `agent-doctor`,
a short one-way fingerprint. Resume rejects a different base URL; only the key
slot may change.

## Deliberate Non-Goals

The first version has no recalculation, refund, resend, replay, order update,
configuration update, database write, service restart, or remediation tool. A
future action plane must be a separate design with explicit human approval and
its own mutation ledger.
