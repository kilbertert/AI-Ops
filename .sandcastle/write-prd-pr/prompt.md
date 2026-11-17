# TASK

Write the title and description for a pull request that delivers PRD #{{PRD_NUMBER}}: {{PRD_TITLE}}.

The PRD ships as a chain of sub-issue runs, all committing to the same
branch. This PR will be reused across every sub-issue run, so the
title and description must describe the **whole PRD**, not any
individual sub-issue. You are NOT implementing anything.

# CONTEXT

Read the PRD and its sub-issues:

```
gh issue view {{PRD_NUMBER}} --comments
gh api repos/$GH_REPO/issues/{{PRD_NUMBER}}/sub_issues
```

Draft the title and description, framed around the whole PRD.
Use `.github/pull_request_template.md` and the stage criteria in
`docs/agents/actions-delivery.md`. Separate implemented, merged, deployed and accepted
results. Classify missing results as pre-merge compatibility blockers or post-deployment acceptance.
The delivery owner keeps or returns the PR to draft for missing pre-merge evidence.
Recheck the body against current evidence before merge; the initial draft is not a result.

# OUTPUT

Based on the PRD and sub-issues you just read, emit a single `<output>` block as the **last thing** in your response:

<output>
{
  "prTitle": "feat: short imperative summary of the PRD as a whole",
  "prDescription": "## changes\n\nWhat the PRD delivers and what remains inactive.\n\n## Sub-issues\n\n- #N — title and evidence\n\n## tests\n\nVerified results and missing acceptance.\n\n## checklist\n\n- [ ] All PRD acceptance criteria evidenced\n- [ ] Independent deployment, compatibility and recovery checked\n\nRefs #{{PRD_NUMBER}}"
}
</output>

Rules:

- `prTitle` must be a single line, under 70 characters, conventional-commit style (`feat:`, `fix:`, `refactor:`, etc.), framed around the PRD as a whole.
- `prDescription` must:
  - describe the PRD's overall intent (restate the goal from the PRD body),
  - list every sub-issue with its number and title,
  - end with `Refs #{{PRD_NUMBER}}`; use `Closes` only after all PRD criteria have evidence
    and merge completes it. Closed implementation sub-issues alone are insufficient.
