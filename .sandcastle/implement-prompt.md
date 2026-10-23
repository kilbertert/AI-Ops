# TASK

Fix issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}} on branch {{BRANCH}}.

Pull in the issue using `gh issue view`, with comments. If it has a parent
PRD, pull that in too. Only work on the issue specified.

# CONTEXT

Generate the repository map, then read it first (where things live):
`node .sandcastle/repo-map.mjs && cat .sandcastle/REPO-MAP.md`. It is gitignored
and produced on demand, so generate rather than assume. then `GLOSSARY.md` (domain language) and the relevant files under `docs/` and any ADRs under `docs/adr/` before
starting. Apply the project's `.sandcastle/CODING_STANDARDS.md`. Explore the
repo and fill your context window with the parts relevant to this issue —
especially test files that touch the area you'll change.
Run the Economy ladder before choosing an implementation; stop at the first
option that fully satisfies the issue and its acceptance contract.

# EXPLORATION

Explore the repo and fill your context with relevant information that will
allow you to complete the task.

# EXECUTION

Let a test lead where a test is the cheapest way to pin the behavior: write it
first, watch it fail, then implement to pass it. That is one technique, not a
ritual. Reach for it where the behavior is non-trivial or a regression would
otherwise be silent — branching logic, parsers, persistence, money, auth,
data transforms.

Do not manufacture a test to satisfy a quota. Prose, formatting, a rename, or
a one-line wiring change owes none. Name the input that would break your
change and pin that input; if you cannot name one, say so in the commit body
rather than writing a test that asserts the implementation back to itself.

While you iterate, run only the narrowest check that can falsify what you just
did — the test files touching your change, the typecheck, the linter. The full
gate below runs everything; run it once, when the change is complete, not after
every edit.

# FEEDBACK LOOPS

Before committing, run `uv sync --extra dev && uv run pytest -n auto && uv run ruff check` (typecheck + tests + build) and
`git diff --check` to ensure everything passes. Then run
`node .sandcastle/policy-check.mjs commit`. Do not weaken or skip checks.

Before you settle on an approach, search what this repository has already
decided: `rg -i "<机制名或关键词>" docs/adr/`, and read the two or three records
whose titles touch the area. Facts the code or those records can answer, answer
yourself — do not spend a human's time on them.

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

If this work came from an outside ticket — a requirements
platform, a chat thread, a support queue — that ticket is where someone is
waiting. Record its identifier and the acceptance criteria this change satisfies
in the commit body under `Source ticket:`. Do not name a pull request: none
exists until the host opens one after this commit, and the host posts that link
to the issue itself. If you are replying without changing code, put the same
`Source ticket:` line in a top-level PR comment instead — there is no commit
to carry it. A result that stays in this repository is one the next person to
open that ticket has to rediscover.

# FINAL RULES

- ONLY WORK ON A SINGLE TASK.
- Once complete, output `<promise>COMPLETE</promise>`.
- If a required human decision, credential, or external environment is
  missing, do not guess; explain the blocker and output
  `<promise>BLOCKED</promise>`.
