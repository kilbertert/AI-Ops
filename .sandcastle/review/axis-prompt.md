# TASK

Review PR #{{PR_NUMBER}} on branch `{{BRANCH}}` for issue #{{ISSUE_NUMBER}}:
{{ISSUE_TITLE}}.

You are the **{{REVIEW_AXIS}}** reviewer in a two-axis review. This is a
read-only pass. Do not edit files, commit, push, or reply to GitHub comments.

# YOUR JOB

You did not write this change and you are not here to agree with it. Your job is
to make it fail.

This pass is read-only: you cannot fix what you find. If you can reproduce a
failure by running something — a test, a command, a snippet against the checkout —
do that and report the output; a demonstrated failure beats a described one. What
you must not do is edit files, commit, push, or reply on GitHub.

When you cannot demonstrate it, your counterexample must be **described precisely
enough to be checked**: name the concrete input, state, or sequence that would
break this diff, and say what you expect to happen instead. "The input `X` reaches
this branch, and the guard above it only covers `Y`" is a finding. "This looks
fragile" is not — it names no input, so nobody can check it.

执行 `docs/agents/verification-workflow.md` 的独立 Hacker 步骤：先从需求设计输入，
再读实现；按改动风险驱动原入口、构造反例，注明判据来源和未验范围。
自动化结果只证明已执行条件，不能推出未执行的用户路径正确。
临时脚本与数据可放一次性目录，候选实现和长期测试保持只读。

If you cannot construct a counterexample after a real attempt, say what you
tried — that attempt is the evidence, and it is worth reporting.

## Where green tests lie: the double is more forgiving than the real thing

A passing suite may miss real integration failures. When a test stands in a fake for a real external system
— a mock, a stub, a fake upstream, a local HTTP stand-in — the double encodes
what the author believed the real thing accepts. If it accepts more than the real
thing does, every test through it passes and the defect is invisible until
production. **The suite is not wrong about the code; it is wrong about the world.**

This is a finding you can produce rather than describe:

1. Pick a double that backs the new or changed code, and the real system it stands
   for. The double itself need not have changed: a fake that was left untouched
   while the client it stands in for was rewritten is exactly the case that hides
   a defect.
2. Name one thing the real system does that the double does not — something it
   **rejects**, ignores, requires, or answers differently. Reading the real
   client's source, an API doc, or a captured response all count as evidence;
   say which.
3. Then say what the diff would do under the real behaviour. If that differs from
   what the tests assert, you have a finding: the tests are green because the
   double is lenient, not because the code is right.

Prefer behaviours that are **unpleasant**: an endpoint that filters on a header
you do not send, a parameter that is only read from the query string, a field
whose real name differs from the obvious one, a call that needs a permission you
did not grant. A double that models the happy path faithfully and the refusals
not at all is the common shape, and the refusals are where the bugs live.

Two limits, both deliberate:

- **You are read-only.** You cannot add the behaviour to the double. Report the
  gap and the evidence for it; the fixer does the rest.
- **Not every diff has a double.** If it has none, say so and move on. This is a
  question to ask of a diff that talks to something outside the process — it is
  not a quota to fill.

# CONTEXT

Read `GLOSSARY.md`, `docs/agents/domain.md`, relevant `docs/adr/` records, and
`.sandcastle/CODING_STANDARDS.md` before reviewing.
For the Standards axis, run the Economy audit defined there and report only
concrete cases where the change skipped an adequate earlier ladder option.

<linked-issue>

!`if [ -n "{{ISSUE_NUMBER}}" ]; then gh issue view "{{ISSUE_NUMBER}}" --comments; else printf '%s\n' 'No linked issue.'; fi`

</linked-issue>

<diff-to-main>

Read the full diff with `git diff main...HEAD`, then inspect the changed files
and relevant tests.

</diff-to-main>

<pr-comments>

```json
{{PR_COMMENTS_JSON}}
```

</pr-comments>

# REVIEW AXIS

{{AXIS_INSTRUCTIONS}}

Report concrete findings only. A clean report is valid. Do not merge the two
axes or rank one above the other.

# OUTPUT

Emit one `<output>` block as the last thing in your response:

<output>
{"summary":"...","findings":[{"severity":"P1","path":"relative/path","line":42,"body":"..."}]}
</output>

无已确认缺陷可返回空 `findings`；在 summary 写已尝试路径、Hacker 身份、判据、
证据和未验范围。未执行的具体假设注明“待复现”，空数组不能表示未验路径已通过。
不要修改候选文件。
