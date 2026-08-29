# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all
operations and infer the repository from `git remote -v`.

## Conventions

- Create: `gh issue create --title "..." --body "..."`.
- Read: `gh issue view <number> --comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
- Comment or label: `gh issue comment` and `gh issue edit`.
- Close only after the implementation or delivery that owns the issue is
  complete.

## PRs as a triage surface

No. External pull requests are not feature requests in this repository.

## Native relationships

PRDs use GitHub native sub-issues. Tickets use native issue dependencies when
available; an issue is unblocked only when every blocker is closed. Do not
replace these relationships with copied planning prose.
