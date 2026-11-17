# TASK

Write the title and description for a pull request associated with issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}.

The implementation is already done — commits sit on branch
`{{BRANCH}}`. You are NOT implementing anything. You are NOT running
tests. You are summarising work that already exists.

# CONTEXT

Read the issue:

```
gh issue view {{ISSUE_NUMBER}} --comments
```

Read what changed on the branch:

```
git log main..{{BRANCH}} --reverse
git diff main..{{BRANCH}} --stat
git diff main..{{BRANCH}}
```

If the diff is large, focus on the commit messages and the `--stat`
summary; only `git diff` specific files when a commit message is
unclear.

Draft the title and description from what you read.
Use `.github/pull_request_template.md` and the stage criteria in
`docs/agents/actions-delivery.md`; cite existing evidence and mark missing checks explicitly.

# OUTPUT

Once you've read everything, emit a single `<output>` block as the **last thing** in your response:

<output>
{
  "prTitle": "feat: short imperative summary",
  "prDescription": "## changes\n\n- Current effect and remaining inactive capabilities\n\n## tests\n\n- Existing evidence and missing acceptance\n\n## checklist\n\n- [ ] Independent deployment and compatibility evidence checked\n- [ ] Recovery, user impact and authoritative docs checked\n\nRefs #{{ISSUE_NUMBER}}"
}
</output>

- `prTitle` must be a single line, under 70 characters, conventional-commit style (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
- Include `Refs #{{ISSUE_NUMBER}}` to retain task ownership while acceptance is unfinished.
  Use `Closes #{{ISSUE_NUMBER}}` only when every acceptance criterion is evidenced and merge
  completes the issue. Record missing results, their owner and the merge/activation scope they block;
  never claim deployment or activation occurred because code is complete.
- Required pre-merge compatibility evidence missing means keep or return the PR to draft.
  Post-deployment acceptance can remain open with `Refs` when the increment itself is proven
  independently deployable. Never close a parent through a completed slice.
