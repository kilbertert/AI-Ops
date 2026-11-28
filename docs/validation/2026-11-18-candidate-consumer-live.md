# 消费者候选应用配套部署与验收

本事件记录P4消费者候选准备；判据为[环境目录Feature](../acceptance/environment-directory.feature)、
[QA D29](../acceptance/environment-directory-qa-plan.md)和[ADR0287](https://github.com/kilbertert/AI-Ops-history-archive/blob/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr/0287-candidate-consumer-frozen-workflow.md)。
不替代管家1.26.0历史回执。真实模型、知识内容、目标环境业务取数及激活仍待验。

## 实现与检查

#1602合并`904472dfd420314742bfe37360239e20767fcf0f`，版本1.27.0；28个维护路径与origin/main逐字一致。
消费者模板来自管理员明确选择的正式Agent快照，保留模型/业务指令，仅绑定目标单库；
首次创建API/site停用，不写应用key、永久对客grant、目标登记或正式发布。Agent直接调用插件，无需dataset。

最终CI `44344023241`：5375通过/10跳过，格式/静态、编译/依赖、wheel/sdist、Gateway镜像通过。
首个完整本地候选5375通过/9跳过；审查修复后的本地全量有一条固定Dify容器45秒超时，
单独原检查复跑通过，CI同项通过；保留超时记录，不将这轮本地全量写为全绿。
Chromium环境页8项通过，完整Webpack构建通过。真实Gateway在一次性根启动，
健康/匿名鉴权/公开规范/退役接口验证通过并清理。首次证据输出在仓外触发脚本relative_to错误，
按支持路径重跑完成；这项工具故障不修改业务协议。

固定Dify镜像实际导入控制器与创建服务验证两候选模式首次停用、普通默认和角色守卫保持。
有界风险检查留分支覆盖/复杂度分析；临时去掉内容漂移守卫的变异被反例击杀，源码恢复。
独立本地Codex Hacker先读需求：6项行为反例、10项恢复/漂移复验和审查修复2项及桥契约通过。
实际模型仅知继承会话配置；DeepSeek harness未安装、CodexHost父会话归属冲突，不声称使用指定模型。
来源模型凭据引用探索初为未确认上游假设，随后显式移除并验证来源保持；未捏造真实凭据泄漏结论。

Devin两条实缺修复：来源按配置的独立路由库读取；提交携带所选版本，已变化返回409、不创建任务。
已回复、解决并请求复审，最终无未处理讨论。另一审查要求的实际Gateway证据已补回读并解决。
AI结果只证明所列反例，不推导业务准确率。

## 配套部署

标准CD `44345704999`部署/发布说明成功；健康`1.27.0+904472dfd420`，
console/schema预检、schema2及188文件摘要通过；annotated `v1.27.0`指向本合并。
Gateway制品摘要`68b8fa2977d85afb04affba9cd2a217b3c64f91499e4a19aead8af32fb016c1b`，
前1.26.0制品与完整备份保留。

自有Dify从固定镜像抽取原文件、核SHA、fuzz=0应用全部补丁，经canonical Compose只重建API/worker。
合并补丁包摘要`06e57d24bfdb0cc39aac45a1c23384636bcac838765bc9a772a7c147ebe0df6b`；
八文件运行字节与只读挂载一致，`aiops_candidate_chatflow_import=1`实际回读，其他容器保持。
管理桥合并整包`69236a36d023b2298dc81755a44d185bd899aab239913a1acb6ded48bd9e9042`，
版本化部署入口及匿名拒绝通过。Web同合并修订完整构建、逐层diff_id/配置摘要校验，两端load通过；
运行镜像`sha256:b276ceab220f604af21dc54546fbbd4c8681a17cab4e830ad3e9069ddd963cf5`，
环境页路由/登录页就绪，其他容器保持。各组件完整恢复点保留；本轮未做生产故障注入回退。

部署后正式route、工作区、六条知识账户、对客grant与应用登记摘要保持；
目标实例已记录配置、installed、目录、数据库、PID及公司容器保持，目标版本仍1.19.0、业务未启用。
不改公司仓库/服务/业务行，不新增客户业务SSH隧道或公网暴露。

## 正常页面验收

首轮人工验证码正常登录200且环境页可见；一次脚本目录fetch网络失败，在消费者任务提交前结束。
失败证据保留；不将该次登录、页面渲染或技术部署当消费者准备通过，也未据此认定鉴权根因。
第二轮正常公司登录200，目录/新计划重复回读通过，后台现场预检及工作区/知识/连接准备observed，复用原资源。
页面明确选择已发布消费者v35，准备结果observed：workspace/knowledge/connection/application passed，
business pending、executable=false。重复点击同任务，刷新同应用且明确“入口停用”。
原生退出200success，旧会话allowed=false、SESSION_LOGIN_REQUIRED、无公司身份；浏览器exit0，管理取证通道已清理。

受控Console另回读候选advanced-chat：API/site均false，API keys为0且没有已发布workflow，
目标知识选择/模型/指令与冻结正式模板装配一致；工作区共原管家和新客服两个候选应用。
插件实际动态选项只返回目标单库。此回读未调用模型，不证明资料内容、业务接口或诊断准确性。
目标为已登记测试租户，非ulink目标；ulink正式v35仅提供已授权编排模板。

原始日志、截图、账户/资源/任务编号、会话材料、备份与全部失败保留私有。
本事件只留此回执，索引按领域/组件/环境引用；完整P4/P5及#1540/#1535保持未完成。
