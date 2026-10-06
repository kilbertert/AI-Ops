## Verification

Project constraints live in `AGENTS.md`. For anything that changes the Gateway's
outward behavior, see its `验证与交付` section: unit tests are not sufficient —
run the `verify-aiops-gateway` skill against a throwaway data root and capture
evidence. Never point it at the real `AIOPS_DATA_HOME`, and never record a device
token in evidence or a commit.

<!-- afk-bootstrap:managed:start -->
## AFK workflow gate

For idea or planning work, read `docs/afk-workflow.md` and the applicable
files under `docs/agents/` first.

`/grill-with-docs` ends only when its frontier is empty: report
`GRILLING_COMPLETE`, summarize the shared understanding, ask the user to
confirm it, and stop. Confirmation completes grilling only. Wait for the user
to explicitly invoke `/to-spec`, `/to-tickets`, `/implement`, or
`/implement-spec`; do not enter another phase automatically. Multi-session
work uses `/to-spec` then `/to-tickets` before implementation.
<!-- afk-bootstrap:managed:end -->
