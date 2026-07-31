# Validation Plan

Synthetic fixtures verify deterministic behavior but do not establish production accuracy.

Production acceptance requires at least three human-confirmed incidents for each supported path:

1. YKC amount or electricity mismatch.
2. Missing transaction data after an order ends.
3. Status 2 uncontrollable exception.
4. Status 5 protocol-reported abnormal end.
5. OCPP server-side billing.
6. Redis downstream synchronization issue.

For every case, compare the generated summary, classification, evidence, and recommended next step with the engineer's final incident conclusion. False certainty is a failure even when the recommended action happens to be correct.
