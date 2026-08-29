# Read-only diagnostic boundary and host-neutral execution

Status: Accepted

## Context

Charging-order diagnosis needs evidence from order, fee, device, queue, and
TDengine sources, but the diagnostic runtime must not become an operational
mutation surface. The same bounded result contract must work for deterministic
fixtures and model-assisted investigation, including remote clients.

## Decision

Expose only bounded read operations through the diagnostic adapters. Order,
fee, device, and Redis evidence comes from the allowlisted `/diag/*` interfaces;
TDengine access goes through the loopback-only read-only proxy. The deterministic
runbook path and the Codex-native Agent path share the same redaction, evidence,
validation, and recovery boundaries. A server-side Gateway owns credentials and
run state; portable clients receive only authenticated result and event metadata.

## Consequences

The runtime cannot perform refunds, recalculation, replay, configuration changes,
database writes, or service restarts. Provider and storage hardening remain
deployment concerns, and any future mutation surface requires a separate design
with its own human approval and mutation ledger.
