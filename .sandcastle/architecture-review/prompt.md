# TASK

You are running the daily architecture-review pass. Find one fresh deepening
opportunity in this codebase and report it as a PRD.

This is an unattended CI run. There is no user to grill, no HTML report to
write. Your job is:

1. List prior proposals labelled `source:architecture-review` (open and
   closed) so you don't re-propose them.
2. Explore the codebase.
3. Pick **one** top candidate.
4. **Report it as structured output — you do not create the issue.** Emit the
   PRD title and body in the `<output>` block specified below; the workflow
   creates the issue from them and applies the provenance label. Do not run
   `gh issue create`, and do not try to apply a label.

The full process — including the methodology (deletion test, deepening,
glossary), the loose-duplicate rule, and the PRD shape — is documented in the
project skill `improve-codebase-architecture-project`. Follow it. The extraction
pass carries the schema; this one just leaves the PRD in prose it can lift.

# CONTEXT

Read `GLOSSARY.md` and `docs/` and any relevant ADRs under `docs/adr/` before proposing
anything. Treat ADRs as binding — do not propose changes that contradict a
recorded decision.

# RULES

- **Read-only, entirely.** No commits. No edits to `docs/`, ADRs, or source
  files. No GitHub mutations either: **do not create, comment on, label, or
  close any issue.** This run's sandbox token cannot perform issue mutations,
  and the workflow performs the one creation this pass needs.
- One PRD per run. If every reasonable candidate is already covered by a
  prior `source:architecture-review` proposal, say so plainly and stop — the
  next pass reports it as `skipped`, so it needs no structured output here.
- No questions to a user — there is none. Make the call.
