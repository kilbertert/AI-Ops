# 业务环境清单上线：新客户开通不再改服务器配置

日期：2026-11-04（北京时间）。制品：`1.0.17+010705a88d05`（PR #1329，CD run 41687518920）。

## 目标

「复制并开通」一个新客户，客户端这一端不再卡在一条只有运维能改的环境变量上。
判据是：在 41 上启用清单之后，对一个**此前没有登记**的租户重跑开通，客户端这一端能走到
`ready / published`，且 `consumer_endpoints` 出现该租户行。

## 已完成内容

- 代码：`consumer.business_environments`（清单读取与形状校验）、
  `approved_business_endpoint()`（环境变量优先、清单接住未登记租户）、
  启动时整份校验、`SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS`。决策见 ADR-0199。
- 41 启用：`/etc/aiops-41/business-environments.json`（0600，owner `aiops41`）+
  `gateway.env` 追加 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE`（`--if-sha256` 写入，
  旧文件备份 `gateway.env.bak-business-env-20261104`）+ 重启。
- 开通复测：作业 `3d3eb447…`（目标优链充，`scope=both`）从 `partial` 走到 `completed`。

## 结果

| 项 | 值 |
| --- | --- |
| 客户端这一端 | `ready`，`published=true`，`version_no=1` |
| `consumer_endpoints` | 新增 `(1941847786056323072, production-41, …/charging-pile, by=setup)` |
| 路由 | `consumer/dify/548b8483… active rev 1` |
| 现役边界 | `/health` 200、openapi 200、`/v1/runs` 404、匿名问答 401 |
| 管家端 | 仍 `draft_only`（已有正式路由，按 ADR-0189 不自动顶替） |

## 未验 / 已知缺口

1. **第一次重试的失败原因未知。** 那一轮 `SETUP_STEP_FAILED` 是兜底码，`self_service.py`
   捕获后不记栈；我在两次重试之间用服务身份单独调过一次 `service_keys.ensure` 以定位，
   该动作本身改变了现场，导致"第一次为何失败"再也无法从现场证明。
   ⇒ 这是本次要修的第二件事：见 PR #1331（失败时把**端、步骤、异常类型**带到页面上，
   并把栈写进 journal）。
2. 客户端应用 `548b8483…` 没有任何真实用户问答；渠道声明仍为空，页面按 `pending` 显示。
3. 管家草稿切正式需要页面上的显式动作，本轮未做。

## 下一步

- 合并 #1331 后在 41 上验证"失败时页面能看到步骤与异常类型"（用一次性数据根 + 真实网关）。
- 需要一次真实 C 端会话跑该客户端应用。

证据与逐项回读见
[2026-11-04-business-environment-enable-and-onboarding](../validation/2026-11-04-business-environment-enable-and-onboarding.md)。
