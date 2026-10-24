# M3-02 节点 SOP 发布绑定

从M3-01合并提交de95f3f6建立feat/m3-node-release。节点目标复用既有运营发布/撤回/恢复面，
新增每run冻结清单读取，Java实际装配及原claims/completions守卫接通。试用使用独立run。
双租户在同一JVM并发使用不同SOP与不同摘要；保存不切版，发布切版，撤回拒绝，恢复旧版。
验证见[本片验收](../validation/2026-10-25-m3-node-publication.md)。
下一步为真实环境准备与有界业务验收；本片不启用生产，不以合成测试宣称业务通过。
