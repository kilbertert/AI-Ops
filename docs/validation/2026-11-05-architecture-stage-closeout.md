# 整体架构文档核对与验证

日期：2026-11-05；基线 `c2316e4a`，候选分支 `docs/architecture-stage-closeout`。
本轮只核对和修正文档，不新跑生产问答、不变更运行配置，不将旧截图重新标成当前版本。

## 判据与证据

- 用户确认的业务链：自然语言/历史→模型按SOP选工具→系统授权和计算→模型回答。
- 源码：Gateway问答入口、consumer_agent/chat_service、Dify最终轮、operator_agent/工具声明/拒绝上报、
  identity能力与范围、会话/运行/发布存储，以及部署/代理资产。
- 真实结果分别引用[客服生产](2026-11-05-consumer-agent-production-closeout.md)、
  [管家原入口](2026-11-05-operator-agent-live.md)、[第二租户](2026-11-05-secondary-operator-route-recovery.md)、
  [拒绝上报补验](2026-11-05-split-callback-gate-f4a-replay-41.md)，不扩展它们的通过范围。
- GitHub开放票与旧台账17项逐项核对（16项已关闭，#602仍开放）；状态已关闭但评论仍写未验的项目单独记入[台账](../reviews/2026-11-05-architecture-stage-closeout.md)。
- 未新增长期测试；使用现有目录、链接/模块索引和退役检查，不以文档措辞锁定测试。

## 本轮检查

- 现有目录、README/模块链接和退役检查：73项通过；git diff --check通过。
- 独立读者复核、CI与合并状态待本轮执行后登记。

## 边界

**未完成业务验收**：本轮没有新增真实故障结论、长期稳定性、全语言/设备、费用完整性或客户独立部署的证明。
原生APK连续会话#1353、Markdown呈现#1422及报告页#602继续开放。
源码事实、历史验收事实与GitHub票据状态分别记账；原始账号、主机映射、凭据和截图不进入本次变更。
