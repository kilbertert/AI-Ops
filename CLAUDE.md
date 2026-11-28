## Verification

重要变更与文档交付遵循 `AGENTS.md` 的 Doc-first 规则及其文档维护入口。

Project constraints live in `AGENTS.md`. For anything that changes the Gateway's
outward behavior, see its `验证与交付` section: unit tests are not sufficient —
run the `verify-aiops-gateway` skill against a throwaway data root and capture
evidence. Never point it at the real `AIOPS_DATA_HOME`, and never record a device
token in evidence or a commit.

新增或移动文件前，执行 `AGENTS.md` 的「目录与模块归属」要求，并查阅其中链接的现行目录规则。
