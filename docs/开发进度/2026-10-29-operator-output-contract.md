# 管家输出契约修复

目标：业务人员正常编写Dify SOP后，管家正式入口和两种试运行能稳定交付结构化报告。

分支`fix/operator-output-contract`，基线`b86be95f1831e24a5611ba300aeac7aa29b0b625`。
本地验收完成后进入PR交付；尚无合并/部署记录。

已完成：

- 阅读指定Claude会话，对照代码确认其“缺少JSON提示词”解释不成立。
- 真Java/Gateway复现缺少供应商输出模式、跨模型轮拼接两种失败；补回归并修复。
- 节点逐轮强制JSON模式，有界收集最后模型轮；保留原Gateway格式、证据与授权校验。
- 154项相关Python测试、12项Java/Gateway整链场景、节点守卫/反向控制通过。
- 锁定JDK/依赖离线构建jar成功；Ruff、Gateway一次性数据根8项HTTP检查通过。
- 新增验收场景、QA计划、ADR0163，更新用户和部署说明，操作方法已入仓。

局限：未核对真实供应商对JSON模式的响应，未部署，未复测ulink实际订单。
未完成业务验收。下一步按PR/CI/合并流程交付节点制品，再按QA计划验证三个真实入口。
详见[本轮验收](../validation/2026-10-29-z10-operator-output-contract.md)。
