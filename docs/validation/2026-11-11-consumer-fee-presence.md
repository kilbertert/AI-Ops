# 零收费与缺项逐字段区分：英文草稿先验与1.7.4候选

日期：2026-11-11；base `02e55376`，分支`fix/consumer-fee-presence`。
父会话确认PR #1472已合并、标准CD至1.7.3且两图发布；本轮先冻结v4并英文预览，再推进1.7.4代码PR，尚未部署或正式发布。

## 真实失败与本轮判据

父会话原H5 English问完整明细时，电费2.53、服务费1.09正确，但答复称
“parking fee and occupancy fee were not provided”；同轮billing实际为`parkFee=0.00`、
`occupyFee=null`。中文草稿未出现此问题，不能据中文通过外推英文语义通过。
输入依据为父会话脱敏说明，作者未读取生产或私有订单。
父会话已通过正常版本API恢复Ulink旧V23/rev31；另一租户保留V3/rev3，当前Gateway1.7.3健康。
这些是父会话提供的恢复状态，作者未执行正式版本操作。

v4仅增加自然语言规则：0/0.00是明确提供的零收费记录，不能归为null/缺项/无效；
parkFee与occupyFee逐字段判断，不能因一项缺失而把另一项零值合并为缺失。
无论语言，每个金额或缺项断言都核对同名来源键；保留已发布v3其余约束及全部运营文字。
不新增parser、validator、审核模型或最终正文覆盖，账务reader和模型配置不改。

## 检查与交接

已发布v3完整文本冻结为迁移输入，保留BASE/v1/v2，现有机制扩展到当前v4。
仅更新已有参数化兼容检查：锁定已发布v3字节、升级/幂等、保留运营前后文、拒绝重复/未知块；
不堆叠措辞字符串测试，不把迁移检查称为自然模型语义验收。
证据位于`dist/verification/consumer-fee-presence/`，包含完整v4文本和冻结hash。
现有flow/migration 27项、目录19项通过；Ruff check/format通过，结果分别为
`flow.log`、`layout.log`、`ruff.log`、`format.log`。已发布v3完整字节与base一致。
完整候选`order-composition-v4.txt`的UTF-8 SHA256：
`b229955ccf22f797afa77f52b116d0e9c4c77eff2807b63b65c6affd48dfe4fd`；源码/迁移工具/测试hash见`freeze.json`。

## 英文授权草稿先验（父会话提供）

冻结v4 SHA256已仅写入两份授权草稿instruction，未更换模型。英文受管预览复用正常Dify session，
设`Accept-Language: en`：原H5完整明细失败问法12.57秒，parking 0.00与occupancy not provided分列，
原数值/币种正确；停车/占位专项10.33秒亦正确。另一租户原Dify中文验收仍在进行，结论待父核读。
manage桥不转发语言头的现有限制与直达受管trial方法已补进[QA](../agents/consumer-wf01/order-composition-qa.md#英文受管预览方法)，
不修改Bridge，所有凭据仅在受控进程runtime读取，不写证据。作者未读取真实订单、session或key。

仅在上述冻结文本英文先验后推进1.7.4版本、文档与PR；v4文字保持不变。独立V3升级Hack由父会话
派发并核报告，本记录尚无该回执。父会话负责后续CI/Devin、合并/CD、正式发布/H5及最终归档。
本地与草稿证据不等于全量业务、所有语言或所有租户通过。**未完成业务全面验收**。

## 1.7.4交付检查

版本同步后flow/migration、billing、目录/文档/版本定向105项通过（`release-focused.log`）。
Ruff check/format、llmdoc validate、wheel/sdist 1.7.4、Gateway镜像构建与无网络只读容器导入通过，
证据为`release-ruff.log`、`release-format.log`、`llmdoc.log`、`build.log`、`image.log`、`container.log`。
冻结v4源文件、迁移工具与测试hash均与英文先验时一致，账务reader无diff；不重复本地全量或路由观察。
本轮只交付PR，完整CI/Devin及之后的合并、CD、正式发布由父会话负责。
