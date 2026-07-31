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

## Business Acceptance Pending

Production acceptance requires at least three human-confirmed incidents for each supported path:

1. YKC amount or electricity mismatch.
2. Missing transaction data after an order ends.
3. Status 2 uncontrollable exception.
4. Status 5 protocol-reported abnormal end.
5. OCPP server-side billing.
6. Redis downstream synchronization issue.

For every case, compare the generated summary, classification, evidence, and recommended next step with the engineer's final incident conclusion. False certainty is a failure even when the recommended action happens to be correct.
