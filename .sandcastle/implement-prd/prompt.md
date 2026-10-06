# TASK

You are implementing one sub-issue of a multi-session PRD.

- **PRD:** #{{PRD_NUMBER}} — {{PRD_TITLE}}
- **This sub-issue:** #{{SUB_ISSUE_NUMBER}} — {{SUB_ISSUE_TITLE}}
- **Branch:** `{{BRANCH}}`

The branch may already have commits from earlier sub-issues. Do **not** rebase
or rewrite that history. Add your work on top.

Pull both issues in for context:

- `gh issue view {{PRD_NUMBER}} --comments` — the full PRD. Read this carefully; your implementation of this sub-issue must fit the larger plan.
- `gh issue view {{SUB_ISSUE_NUMBER}} --comments` — the specific step you are implementing now.

You also have access to the full list of sibling sub-issues:

`gh api repos/$GH_REPO/issues/{{PRD_NUMBER}}/sub_issues`

Use this to understand what work has already shipped on this branch and what
is still ahead — but **only implement #{{SUB_ISSUE_NUMBER}}** in this session.
Do not touch work that belongs to a different sub-issue.

# CONTEXT

Read `GLOSSARY.md` and the relevant files under `docs/`, apply `.sandcastle/CODING_STANDARDS.md`, and any ADRs under `docs/adr/` before starting.
Explore the repo and fill your context with the parts relevant to this
sub-issue — especially test files that touch the area you'll change.
Run the Economy ladder before choosing an implementation; stop at the first
option that fully satisfies this sub-issue and the parent PRD contract.

# EXECUTION

Use red-green-refactor where applicable.

1. RED: write one failing test
2. GREEN: implement to pass it
3. REPEAT until the sub-issue is done
4. REFACTOR

Before committing, run `uv sync --extra dev`, `uv run pytest`, and
`uv run ruff check`, then `node .sandcastle/policy-check.mjs commit`.

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

Make one or more git commits on `{{BRANCH}}`. Use conventional-commit
messages (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`). Do NOT use a
`RALPH:` prefix.

Include `Part of #{{PRD_NUMBER}}` in each commit body so the history is
linkable from the PRD. Do **not** include `Closes` in commits — closing the
sub-issue is the workflow's job, and closing the PRD is the merged PR's job.

Do not close the sub-issue yourself. Do not push the branch. The workflow
handles both.
