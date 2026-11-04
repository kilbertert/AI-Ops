# 地点查询真实路径验收与部署

对应[契约](site-query-real-path.feature)、[ADR-0210](../../adr/0210-site-query-plan-precedes-tool-evidence.md)。

| 编号 | 环境、输入与操作 | 可观察结果 |
|---|---|---|
| SQ-01 | 一次性Gateway，CPMS严格子串过滤；原问题“广州天河”，节点调用数组词 | 真授权→代理→证据→结果包含实际匹配站，不再只查整串 |
| SQ-02 | 数组过长、空词、非字符串、编造地点、新旧参数同时给出 | 取证前拒绝，不请求业务系统 |
| SQ-03 | 第二词读取时混入其他租户/站点或撤权 | 整轮不交付，不保留部分成功结果 |
| SQ-04 | 多词重叠、50行和后续页；消费者含非本人站 | 去重、截断语义与Java一致，消费者只保留本人范围 |
| SQ-05 | 真Java+Gateway、合成模型/CPMS | Java数组输入、声明schema、参数冻结与最终证据一致 |
| SQ-06 | 真实模型，使用本次工具定义与原问法，3次 | 记录调用参数；不得把生成了参数当作站点查询通过 |
| SQ-07 | 合并后原Dify/H5，各提交同一句 | 页面实际显示本次授权站点，关联本次QA和运行版本，独立CPMS对照 |

SQ-01–04：`uv run pytest tests/operations/test_site_query_real_path.py tests/consumer/test_consumer_tool_grants.py`。
SQ-05沿用[输出契约脚本](output-contract-qa.md)，站点场景现在实际发起Java工具调用。
SQ-06先确认本地import指向任务worktree（用uv run），再把当前schema与提问通过受控
模型连接调用；密钥现场读取且只输出工具名/参数，禁止打印响应中的凭据或账户资料。

## 部署顺序

按[正式节点手册](../../../deploy/node-runtime/README.md)和[环境手册](../env-41-runbook.md)：

1. 只读保存路由/版本、节点镜像与jar/工具声明摘要，确认没有在途run；备份旧release、runtime.env
   和node/operator-tools.json。构建必须从已合并修订归档，核jar与锁的摘要一致。
2. 用dev-host传有摘要的jar与同修订operator_tools.json，建立新release；保持服务身份与权限，
   只定点重建node，其他容器不动。新旧声明与jar一并恢复，不能仅回退其中一个。
3. 节点就绪后走标准Gateway CD。两端部署窗口不发测试请求，能力报告的工具声明摘要须一致。
4. 核对现役清单：订单/消费者清单没有cpms_site_search时无需重发；无订单管家由运行时生成
   新schema。模板中已含站点工具时按当前声明更新模板，已发布旧schema走正常发布并记录审计。
   不改业务SOP，不换模型，不借发布绕过权限，不改写历史run。
5. 原Dify预览与原H5验SQ-07；只读重查同租户的/chSite/page，始终带当前身份与tenant-id。
   缺tenant-id时上游可能返回空，不能据此下“没有站点”结论。记录同一次QA与实际CPMS参数。
6. 部署失败恢复节点jar/声明和对应Gateway旧版本，保留新产生的QA与取证；不回滚业务数据库。

本地测试结束清理一次性数据根，证据留dist；生产截图、主体、站点详情仅保存在私有证据根。
此验收不证明任意地名拆分、跨语言匹配或地理距离排序。
