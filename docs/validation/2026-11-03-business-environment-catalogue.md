# 业务环境清单：让「复制并开通」不再需要改服务器配置

日期：2026-11-03。分支 `feat/business-environment-catalogue`（PR #1329）。
决策见 [ADR-0199](../adr/0199-business-environments-are-approved-per-deployment.md)。

## 目标

新租户双端开通时，客户端这一端卡在 `SETUP_BUSINESS_CONNECTION_REQUIRED`：
批准源只有 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 的**按租户映射**。把批准上移到
"按部署"的清单，使新客户开通不再改 `gateway.env`、不再重启网关。

## 已完成内容

- `src/aiops_diagnostics/consumer/business_environments.py`：清单读取与形状校验
  （schema、id 唯一、至多一个 default）。
- `approved_business_endpoint()`：环境变量里该租户的条目优先，其次清单里唯一地址。
- `gateway_config.validate()`：清单在**启动时**整份校验（含 `canonical_gateway_url`）。
- 新拒绝码 `SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS` 与对应页面文案。
- 清单只校验形状、不自己实现 URL 规则：`business_environments` 不导入
  `gateway_config`，避免新增包内循环边。

## 验证结果

| 项 | 结果 |
| --- | --- |
| `ruff check` / `format --check` | 通过（395 文件） |
| 本机全量 `pytest -n 4` | 通过（`test_readme_architecture`、`test_m2_live03_deploy` 的临时红已修：前者加了模块文档与去掉新边，后者在最终树上重建并重锁 `images.lock.json`） |
| `verify-aiops-gateway`（一次性数据根） | 8 项全过，`dist/verification/business-environments/aiops-gateway.json` |
| 清单运行时证据（本文件同目录 `business-environments.json`） | A 合法清单 ⇒ `serve` 起来、`/health` 200；B 远端 `http://` 地址 ⇒ **启动即失败**且错误信息点名清单 |
| CI `verify` | run `41685298563` success |

证据脚本：`tools/verification/business-environments/observe.py`（一次性根、不读写真实数据根、
不含任何令牌）。

## 仍未验

- **41 生产启用未做**：需要写 `/etc/aiops-41/business-environments.json`、在 `gateway.env`
  增加一行、重启网关。步骤见
  [业务环境清单手册](../agents/operator-cpms/business-environments-catalogue.md)。
- **真实页面开通未复测**：优链充那一次 `SETUP_BUSINESS_CONNECTION_REQUIRED` 的复现
  与修复后的一键开通，都要在启用后从页面重跑一条才算数。本地证据只证明"批准源与
  启动校验按设计工作"，不证明 Dify 侧复制/发布链。
- **回滚语义**：清空清单路径只停止**新**登记；已写进 `consumer_endpoints` 的租户继续用
  那一行（Devin Review 指出）。回滚若要让某租户失去连接，必须同时删该登记行——这一步
  没有自动化，是运维动作。
