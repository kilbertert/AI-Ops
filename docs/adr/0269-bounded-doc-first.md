# 269. 文档按权威来源更新，进度与验收不重复记录

状态：已采纳

## 决定

文档维护流程以[文档导航](../README.md#变更前与交付时)为唯一正文，AGENTS只保留触发入口。
替代[ADR-0097](0097-milestone-records-are-one-file-each.md)的强制双记录和按文件名选最新做法；
保留独立验收事件一文件、旧日志冻结及失败证据。每个Issue承载自己的进度，必要回执不再另抄摘要。
现行契约、提案、决定、操作步骤、运行证据与检索投影分别负责，不由代码自动改写获批需求。

## 替代方案

- 保持双记录：每次都能找到进度和验收，但维护两份事实且不能由文件名判断领域与适用版本。
- 整套安装Trellis/OpenSpec：可统一任务和上下文，却与既有AFK、Feature/QA、ADR、llmdoc重叠。
  采用其上下文选择和增量整合思路，复用现有入口，不引入另一条任务/自动提交轨道。
- 全量删减历史：能立刻减少文件数，却损失失败证据；改为当前入口不默认加载历史。

## 后果与验证

README改为职责目录入口，源码地图按需生成；现有检查仍保护可发现性、链接和导入边界，
不再要求每个实现文件写进顶层README。影响报告复用llmdoc映射；关联源变化是复核提示，不能证明文档错误。
先在环境目录与开通试点，按[文档流程Feature](../acceptance/doc-first.feature)独立演练，后续任务记录效果。
试点改变维护方式，不减少业务验收、安全门禁或私有证据义务，也不宣称全库文档均已校准。

参考：[Trellis工作流](https://github.com/mindfold-ai/Trellis/blob/f089cb3286071199e84118dee86b6d76aab032f1/packages/cli/src/templates/trellis/workflow.md)、
[OpenSpec契约与增量](https://github.com/Fission-AI/OpenSpec/blob/ea635dd255f8686dcf48626bbcbe0e3fd59b6067/docs/concepts.md)。
