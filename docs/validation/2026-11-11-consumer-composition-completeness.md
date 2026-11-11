# 订单构成开头完整性表述：草稿先验与1.7.3候选

日期：2026-11-11；base `5dd35a9c`，分支`fix/consumer-composition-completeness`。
父会话确认PR #1471合并并标准CD至1.7.2；冻结v3已先在授权草稿预览，本轮推进1.7.3代码PR，尚未部署或正式发布。

## 真实失败与本轮判据

父会话原Dify v2复验数值、币种、null与分时已正确，但开头仍称“各项费用都有记录……
订单本身各项收费都有对应记录，您可以放心核对”，与详情占位费未提供矛盾。
该反馈来自父会话脱敏说明，作者未读取生产或私有源；先概括齐全再补缺项不算正确表达。

v3仅增加开头完整性限制：只总结已取得的具体分项并同时点出缺项；存在null、missing、invalid、
币种缺失/无效/冲突或截断时，不泛称记录齐全，不以“放心核对”作完整性保证。
保留全部v2费用、币种、来源起止、禁止合计/差额归因等规则及自然回答，账务reader、模型配置不改。

已发布v2完整文本冻结为迁移输入；现有迁移扩展到BASE、v1、v2、当前v3，
仍只移除完整已知块、保留运营文本、将当前规则置尾，重复/未知标记拒绝。
现有参数化检查增加已发布v2升级及字节合同判据；不堆叠字符串措辞测试，不冒充模型语义验收。

## 本地检查与交接

flow/migration及Ruff结果随冻结候选记录在`dist/verification/consumer-composition-completeness/`。
现有flow/migration 25项通过，Ruff check/format通过；已发布v2字节hash与base原文一致。
`order-composition-v3.txt`为UTF-8完整规则文本，SHA256为
`ba5a4472490c4cce1d349eaead1d4086580f60aa5bce271eaf3c8453e1f2fd4c`；源码/迁移工具/测试hash见`freeze.json`。
冻结v3文本交给父会话预览后保持不变，版本四处同步1.7.3；作者不调用生产、模型或正式发布入口。

## 授权草稿先验（父会话提供）

两份真实授权草稿只更新instruction，采用上述完整SHA256的v3后走原Dify预览。
Ulink耗时14.19秒，父会话已逐句核读：开头明确币种缺失与占位费未提供，只概括主要已取得分项，
原费用/分时逐值一致，不再保证各项全部齐全或“放心”；额外status工具调用已核finished。
另一租户耗时14.16秒，正文最终核读仍待父确认；不能仅用completed视为语义通过。
这是先冻结、再草稿预览、再代码发版的顺序，未触及正式发布指针；两份原始草稿/业务数据未提交。

1.7.2原失败及前一轮过早正式发布后的正常恢复记录仍保留；本轮只证明已明确列出的草稿结果，
不据此推导业务准确率、多租户完整覆盖、长期稳定性或原H5已验收。**未完成业务全面验收**。
父会话负责合并、标准CD、正式发布及H5；独立迁移Hack结果尚待回执，作者负责PR CI/Devin闭环。

## 1.7.3交付检查

版本同步后flow/migration、billing、目录/文档/版本定向103项通过（`release-focused.log`）。
Ruff check/format、llmdoc validate通过；wheel/sdist 1.7.3、Gateway镜像构建及无网络只读容器导入通过，
证据为`release-ruff.log`、`release-format.log`、`llmdoc.log`、`build.log`、`image.log`、`container.log`。
冻结v3源文件、迁移工具、测试hash与草稿先验时相同；`consumer_billing.py`未修改。
不重复本地全量或Gateway路由观察（路由/鉴权/响应未改）；PR CI运行完整检查，最终结果另记。
