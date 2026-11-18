# 候选运行接线工件部署与导出回执

判据：[环境目录Feature](../acceptance/environment-directory.feature)、[QA D30](../acceptance/environment-directory-qa-plan.md)、
[ADR0288](../adr/0288-environment-runtime-artifact.md)。本事件记录运行配置准备，**不表示目标已安装或业务已启用**。

## 实现与验证

#1604合并`0218299f53c05e0a55caada09124db3249c30bb1`，版本1.28.0；13个维护路径与origin/main逐字一致。
原运维入口environment-runtime以同计划新鲜双端观察和明确专属节点配置生成私有工件，
复用节点/目标格式、批准模型、仓内SOP/11工具和指定知识库，不写正式route/key/grant或准入。

最终CI `44359677243`：5386通过/10跳过；本地5387通过/9跳过。
格式/静态、依赖、wheel/sdist、镜像及一次性真实Gateway启动/鉴权检查通过。
初候选有界分支观察93%语句/74%分支并留复杂度分析；不把覆盖率当业务正确性。

独立本地Codex Hacker18项既有及6反例发现H1：密钥列表缺data被误当零key。
修复为仅明确空列表通过，作者6项重放及独立11项复验通过；缺失/null/非空/错误形状均拒绝。
临时撤掉严格列表守卫的变异被最小反例击杀，源恢复。历史H1失败保持私有，不改写为通过。
审查另确认过期观察能被同内容新计划借用，修复为任务当前observed且计划编号一致；
独立6项复验覆盖两端过期/不同plan、原缺列表拒绝和正常导出。Devin首次无发现、复审完成，
审查线程已回复/解决。Hacker实际模型仅知继承配置，不声称使用未安装的DeepSeek harness。

## 标准部署

北京时间2026-11-19 01:37，标准CD `44360658460`部署及发布说明成功。
健康`1.28.0+0218299f53c0`，console/schema预检、schema2及189文件摘要通过；
annotated `v1.28.0`指向本合并。制品摘要`79d2e08b678b55f91a264111968522e2fded4626a6c6df3d94fec6d0a57ac57b`，
前1.27.0制品/完整备份保留。本片只更新控制Gateway/运维入口，管理桥/Web和Dify补丁维持1.27.0合同。
没有目标安装、公司服务/业务行、专属业务SSH隧道或公网暴露变化；生产故障注入回退未验。

## 真实资源与工件

北京时间01:39–01:40，现役运维入口新建冻结计划并完成目标现场观察，
原工作区/空库/受限连接/管家与客服分别重新observed；复用原资源不重复创建，双端同一计划编号。
受控Console只读回读停用入口、明确空密钥列表、模型/SOP/工具/知识配置后，
environment-runtime成功导出私有工件，外层及每成员摘要验证通过。

工件绑定目标环境node-124、两个原候选应用、独立工作区/单库，管家模型为
deepseek-v4-1-flash-261017、11项工具；节点出口使用现有批准配置，不复制控制环境执行目标。
公开回执resource-scope/node-binding/application-content passed；
consumer-callback、knowledge-content、installation、candidate-runtime、business、activation仍pending，
executable/installed/activated均false。秘密仅在私有0600成员中，不回显到回执。
本轮是原CLI＋真实资源导出，未新增正常浏览器登录或调用模型；11月18日页面准备证据保持独立。

前后正式route、工作区、知识账户、对客grant和应用登记摘要一致；目标配置、数据库、PID和公司容器保持。
完整工件、任务/账户编号、现场配方、备份、日志与失败只保留私有。后续由受管安装事务接入此工件，
再验证目标身份、消费者回调、真实模型/工具与三业务；完整P4/P5、#1540/#1535仍open。
