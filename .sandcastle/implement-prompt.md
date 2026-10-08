# TASK

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}} on branch {{BRANCH}}.

Pull in the issue using `gh issue view`, with comments. If it has a parent
PRD, pull that in too. Only work on the issue specified.

# CONTEXT

Read `.sandcastle/REPO-MAP.md` first (where things live), then `GLOSSARY.md` (domain language) and the relevant files under `docs/` and any ADRs under `docs/adr/` before
starting. Apply the project's `.sandcastle/CODING_STANDARDS.md`. Explore the
repo and fill your context window with the parts relevant to this issue —
especially test files that touch the area you'll change.
Run the Economy ladder before choosing an implementation; stop at the first
option that fully satisfies the issue and its acceptance contract.

# EXPLORATION

Explore the repo and fill your context with relevant information that will
allow you to complete the task.

# EXECUTION

Use red-green-refactor where applicable:

1. RED: write one failing test
2. GREEN: implement to pass it
3. REPEAT until the issue is done
4. REFACTOR the code

# FEEDBACK LOOPS

Before committing, run `uv sync --extra dev && uv run pytest && uv run ruff check` (typecheck + tests + build) and
`git diff --check` to ensure everything passes. Then run
`node .sandcastle/policy-check.mjs commit`. Do not weaken or skip checks.

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

Make git commits on `{{BRANCH}}` with **Conventional Commit** messages
(`feat:`, `fix:`, `refactor:`, `test:`, `docs:`). Reference the issue in the
body. Keep the diff focused.

Do **not** push, merge, or close the issue, and do not modify GitHub state —
the planner loop handles delivery.

# FINAL RULES

- ONLY WORK ON A SINGLE TASK.
- Once complete, output `<promise>COMPLETE</promise>`.
- If a required human decision, credential, or external environment is
  missing, do not guess; explain the blocker and output
  `<promise>BLOCKED</promise>`.
