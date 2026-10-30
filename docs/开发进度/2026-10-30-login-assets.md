# 验收登录账号入库外加密清单

分支 `docs/login-asset-runbook`，基线 `f37f5fa7`。纯文档 + 一次资产迁移，无代码改动。

## 已完成

**资产迁移**：4 条可复用验收登录资产写进库外加密清单
`~/.config/dev-host/secrets-age/secrets.age`，按既有「reusable login asset」的 JSON
schema（`asset_id` / `project` / `environment` / `kind` / `username` / `password` /
`verification` / `purpose` / `source` / `recorded_at`）。

- 旧密文备份：`secrets.age.bak-aiops-login-assets-20261030-082857`
- 明文临时文件已 `shred -u`，解密回读确认 5 条资产（含原有 1 条）

**方法入仓**：`docs/agents/env-41-runbook.md` 新增 §0b，只写位置、读取命令、条目
schema 与新增流程；引用资产的地方只写 `asset_id`。

**顺手清掉公开仓库里的手机号**：`docs/adr/0090` 与归档 `docs/validation.md` 里各有一处
真实手机号，仓库是公开的。两处改成引用 `aiops-ulink-phone-20261030`；结论与数字未动。

## 缺口与边界

- 资产**值**只在加密清单里；本仓库任何地方都不含手机号、账号或口令（已 grep 核对）。
- 归档 `docs/validation.md` 原则上只读，本次只换标识写法，不改结论。
- 未做：把这 4 条资产接到任何自动化验收脚本里——那是「真实业务验收」那张票的事。

## 下一步

「真实业务验收」独立成票：用这些资产在 41 上跑一次端到端，产出可复现的业务结论。
