# 受管无订单试用现场修复

PR #1154已合并`2da807b4`并部署Gateway/Java节点/桥；首次真实ulink无订单试用失败。
run已冻结正确的无工具清单，但受管试用绑定不含正式manifest_text，回调提前转换空JSON，
发生在模型调用前。未将该次部署写作业务通过。

本轮将两处正式清单转换移进非trial分支；空绑定回归已先红（failed），保留全部试用授权检查。
基线`2da807b48a565dc9bfd4143b0e6dd0498704d053`，分支`fix/operator-managed-conversation`。
专项87 passed，Ruff/格式与内容锁重建通过；远端CI待验证，仍未完成业务验收；部署后继续原ulink三入口验收。
