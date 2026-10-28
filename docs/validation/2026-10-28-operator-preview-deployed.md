# 原生管家预览部署与正式 Java 路由开放

日期：2026-10-28（北京时间）；实现 [PR #1122](https://github.com/kilbertert/AI-Ops/pull/1122)，
合并修订 `812757dd84c471dbd810343671be4096aa09da24`。本回执分支 `docs/operator-native-preview-live`。
**未完成业务验收**：本记录严格区分已部署、已开放与真人订单成功。

## 已完成

- Gateway CD run 40669616941 成功，health 为 `0.2.0+812757dd84c4`，business_mutations=disabled。
- 控制台桥从同一合并提交构建并部署，制品 SHA-256：
  `38e382899de9ed9ddbfc09bc465ace399d283532543f3f07f50182c96ce0bf52`。
  包内各文件摘要通过，nginx 配置检查通过，桥和登录代理 active。原生接线脚本只注入一次。
- 两条新预览路由现场无会话请求均 403，返回 `OPERATOR_PREVIEW_REJECTED`，没有透出凭据。
- 通过原 `/v1/dify/publish` 恢复原管家应用：内容版本22无需重建，路由由 retracted/revision2
  变成 **active/revision3/engine=node**；按用户本次授权持续开放，不再使用临时开窗后的撤回。
  发布前后两条 consumer 路由逐字段相同；Java node、platform、model-proxy 均在线。
- 原工具、公司登录身份、业务权限、模型 key 与发布内容没有改成共享授权；明确订单要求保留。

## 验证层级

| 层级 | 结果 |
|---|---|
| 最终本地完整检查 | 4227 passed / 9 skipped |
| 最终 CI | 4226 passed / 10 skipped；verify、Workflow policy、spec-tree通过 |
| 原生预览关联测试 | 审查后75项通过；无 provider_name、其他提供方同名工具、非法混合订单、跨应用取消覆盖 |
| 真实 Java 隔离链 | 正常两工具、供应商失败、截断、未保存草稿试用、全关拒绝五组通过；身份/模型/CPMS为合成 |
| 浏览器与真桥回放 | 缺订单零任务；节点报告在兼容的原生 SSE 显示模型中可见；不等于生产用户浏览器 |
| 生产发布与健康 | 已完成，active/revision3，未改变 consumer 路由 |
| 生产正常登录+指定订单预览 | 18:17:45公司登录200；18:17:50原生桥200并创建任务；Java约21秒完成，QA completed/run succeeded、node-41/matched、两项真实CPMS证据 |

测试订单沿用用户在本会话明确同意的现有订单，完整编号仅在私有证据中保留。
两次先前失败分别为模型配置冲突和无任务授权，不复用其旧结果当本次成功证据。

## 正常公司登录后的真实结果

用户确认已重新登录并发送后，按具体 QA/run 对账，不按最近任意成功行判断。
任务创建 10:17:50.596 UTC，完成 10:18:11.818 UTC；实际/期望节点均 node-41，
执行清单 expected/observed 相等，snapshot.trial=true；正式路由保持 active/revision3。
Java 日志同 run 接收后20.481秒完成。订单详情和充电监测均为本 run 成功证据。

本单状态2，停止描述为后台检测异常；采样接口返回0条，电量与结构化停止原因码为空。
报告明确将这些写为缺失证据，未声称看过日志/知识库或推断无故障。故障原因仍需业务核验。
本轮已证明真实公司会话→原生预览桥→Gateway单订单授权→Java→双CPMS→完成结果；
用户随后明确确认原生预览面板已显示“Java 节点试用”开头的完整报告，含采样0条与缺证说明，
因此本次真人原生页面展示也已通过，不只依据后台状态。
没有在本轮另做真实跨租户单或点击停止反例，其边界证据仍来自隔离测试。

## 操作与恢复

方法见 [原生预览手册](../agents/operator-cpms/native-preview.md) 与
[桥部署手册](../../ops/dify-console-bridge/README.md)。36 备份为
`/var/backups/aiops-console-bridge/native-preview-812757dd`，含旧 Python、JS、nginx snippet；
部署制品目录 `/opt/aiops-console-bridge/releases/812757dd`。
Gateway 走原 CD，桥经 `dev-host cp --artifact-sha256` 后只运行包内 deploy.sh，未手改服务源码。
不重启登录代理，保留有效会话；没有新增自动撤回 timer。

正式路由可用既有 retract/restore 恢复；成功后不擅自撤回已获持续开放授权的路线。
应用配置与全部路由的执行前快照、发布后回执保存在操作人的
`~/.cache/aiops-operator-native-preview/`；该目录0700，凭据不进仓库。
本轮发布不代表公司管家前端 MR 已部署，也不证明故障结论或业务准确率。
