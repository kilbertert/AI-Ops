# Validation Plan

Synthetic fixtures verify deterministic behavior but do not establish production accuracy.

## Infrastructure Boundary Verified

The production access path was verified on 2026-07-31 with dedicated identities and no business-data
mutation:

- `aiops doctor` connected successfully to MySQL 8.4.7, TDengine 3.4, and Redis 6.2.7 through the
  restricted SSH tunnel.
- MySQL reported only `USAGE` plus `SELECT` on the three diagnostic tables; a query against an ungranted
  business table was denied.
- The TDengine proxy returned both required stable names and rejected a DDL request with HTTP 403. Direct
  SSH forwarding to the native TDengine REST port was administratively prohibited.
- Redis allowed the bounded Stream metadata/read commands and denied access to a key outside the two
  configured Stream patterns.
- The SSH identity rejected shell execution and permits local forwarding only to the approved MySQL,
  TDengine proxy, and Redis endpoints.

This verifies connectivity and the permission boundary. It does not validate diagnostic conclusions for
real incidents.

## Automated And Read-Only Replay

The business hardening pass on 2026-07-31 completed without business-data mutation:

- 88 automated tests passed, including 17 targeted regressions and a 40-case protocol/status/launch-type
  matrix.
- The production TDengine schema was checked directly. Camel-case columns require quoted identifiers;
  the restricted proxy and runtime now use the same exact allowlisted query.
- Redis Stream payloads contain non-UTF-8 bytes. The runtime now matches order numbers against bounded raw
  bytes and exposes only counts and metadata; a retained production message was matched successfully.
- A bounded 30-day replay covered 1,012 orders. Operator orders with stored `tx_data` no longer become
  `missing_tx_data`, and their special billing path no longer produces the previous broad amount mismatch.
- Production samples covered operator YKC1.8, remote YKC1.6, OCPP, AYK, two-wheel, and status-2 paths.
  MySQL, TDengine, and Redis all completed without source failures.
- The replay found 178 operator orders whose status says normal finish while the YKC stop code says abnormal.
  The backend operator path sets status 1 after its abnormal-status handler, so the diagnostic report now
  surfaces this as a status/stop-reason inconsistency instead of silently accepting it.
- The production TDengine proxy still rejected a DDL request with HTTP 403 after deployment.

This replay establishes rule consistency against the sampled 30-day data. It is not a substitute for an
engineer's confirmed incident conclusion.

## Business Acceptance Pending

Production acceptance still requires at least three human-confirmed incidents for each supported path:

1. YKC amount or electricity mismatch.
2. Missing transaction data after an order ends.
3. Status 2 uncontrollable exception.
4. Status 5 protocol-reported abnormal end.
5. OCPP server-side billing.
6. Redis downstream synchronization issue.

For every case, compare the generated summary, classification, evidence, and recommended next step with the engineer's final incident conclusion. False certainty is a failure even when the recommended action happens to be correct.

## Codex-Native Harness Validation (2026-08-03)

The thin-harness implementation was validated without business mutation:

- The automated suite covers immutable incident identity, private workspaces,
  evidence hashes, PII/secret redaction, bounded tool dependencies, result
  validation, malformed output repair, timeout/provider interruption, resume,
  and pluggable API key slots.
- Three independent scripted-agent runs for each YKC amount mismatch, missing
  transaction data, and OCPP server-billing fixture produced the same incident
  identity, tool sequence, evidence IDs, conclusion class, confidence, and
  limitations. Scripted turns validate harness repeatability; they are not a
  substitute for a real model's business judgment.
- Failure injection covered a TDengine outage, malformed structured output,
  a timed-out turn, and a provider-side failure. Failed evidence remained in
  the journal, high confidence was rejected, and the same thread/run remained
  resumable.
- Production `agent-doctor` dependencies remained read-only: MySQL 8.4.7
  reported no unsafe privileges or unresolved roles, TDengine exposed both
  required stables through the strict proxy, and Redis 6.2.7 was reachable.
- A bounded 30-day aggregate sample returned 1,252 candidate rows. Ten
  representative paths covered YKC1.8, YKC1.6, OCPP, HLHT, HW104,
  operator/remote/admin launches, order types 0/1, and statuses 0/1/2/3/5.
  All ten deterministic shadow reports completed without a failed source.
- The thin evidence pipeline was then run against three sanitized production
  paths (YKC1.8, YKC1.6, OCPP). All six tools completed for each path; every
  artifact hash verified, workspaces were mode `0700`, and exact runtime
  database/API secrets were absent from staged references, events, journals,
  and evidence artifacts.

The configured Responses API endpoint accepted the provider key and returned
an explicit `429 INSUFFICIENT_BALANCE` response. The real Codex turn therefore
could not complete. The failure was delivered without a traceback or secret,
the run state became `interrupted`, and `agent-resume` reused the same thread.
Real-model business validation remains pending provider balance restoration or
selection of another funded key slot, followed by engineer-confirmed incidents.
