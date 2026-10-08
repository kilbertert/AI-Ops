# TASK

Implement issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

You are on branch `{{BRANCH}}`, already created from `main`. Pull in the
issue with `gh issue view {{ISSUE_NUMBER}} --comments`. If it has a
parent PRD, pull that in too.

# CONTEXT

Read `.sandcastle/REPO-MAP.md` first (where things live), then `GLOSSARY.md` and `docs/`, `.sandcastle/CODING_STANDARDS.md`, and any relevant ADRs under
`docs/adr/` before starting. Explore the repo and fill your context with the parts
relevant to this issue — especially test files that touch the area
you'll change.
Run the Economy ladder before choosing an implementation; stop at the first
option that fully satisfies the issue and its acceptance contract.

# EXECUTION

Use red-green-refactor where applicable.

1. RED: write one failing test
2. GREEN: implement to pass it
3. REPEAT until the issue is done
4. REFACTOR

Before committing, run `uv sync --extra dev && uv run pytest && uv run ruff check`, then
`node .sandcastle/policy-check.mjs commit`.

This repo HAS a project verification skill, `.claude/skills/verify-aiops-gateway`.
If your change alters the Gateway's outward behavior (routes, auth, enrollment,
response shapes), run that skill and capture its evidence before committing.
Tests are not a substitute: it runs the real gateway on a throwaway data root and
drives it over HTTP. **Never point it at the real AIOPS_DATA_HOME, and never
record a device token in evidence or a commit.** It cannot prove `/diag/*` real
diagnosis, model-backed runs, or KB liveness — say so rather than implying
coverage. If you cannot run it, say so and why in the commit body; do not report
it as done.

# COMMIT

Make one or more git commits on `{{BRANCH}}`. Use conventional-commit messages (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`). Do NOT use a `RALPH:` prefix — that prefix is reserved for the RALPH loop.

Do not close the issue yourself.
