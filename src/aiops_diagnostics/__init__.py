"""Read-only charging order diagnostics."""

# CD 部署时会在**暂存副本**里把这一行改写成 `<版本>+<commit-short-sha>`
# （见 deploy/deploy-41.sh 的「注入 commit 标识」）。`/health` 的 version 字段取自
# 这里，所以部署后能直接回答「现在跑的是哪个 commit」—— 回滚也因此可验证。
# 工作树里的这一行始终保持裸 semver，不被改写。
__version__ = "0.1.0"
