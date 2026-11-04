# 站点查询“修过仍为空”的根因与回归

日期：2026-11-04；基线`da673ecb`。本轮查阅指定Claude会话的本地记录，宿主会话接口缺连接，
没有向该会话发送操作指令。原始会话、模型和业务数据保持私有。

## 根因证据

已部署#1351的精确失败QA：状态succeeded，站点证据输入为单字符串广州天河，0行，
最终答复词组却是广州/天河。只读CPMS对照（带当前身份与tenant-id）：整串0行，天河1行。
返回地址混用汉字与英文，省市区编码为空；不能从不存在的编码构造行政层级过滤。

真实代码路径为节点授权→OperatorCpms.proxy→cpms_get→site_tool_result，绕过query_sites。
#1351只改了后者和本地匹配，真实工具仍传整串；数组直到最终答复才出现，已错过取证时机。
测试替身没有执行地址过滤，且整链测试将地址写成“广州天河测试路”，所以错误路径仍能通过。

修复：调用工具前传address_terms，语义源于用户完整原词；代理和直接查询共用有界读取。
Java工具参数支持数组，声明/schema/证据输入同源。最终答复核对冻结词组，不能取证后改地点。
旧字符串仍为字面匹配，不加城市特例或任意滑窗。tenant-id原生产链已发送，不是本次代码缺失。

## 验证

- 原红测：`uv run pytest tests/operations/test_site_query_real_path.py -n 0 -x`，3.10秒失败，
  真Gateway工具链只向严格CPMS替身发整串，页面报告无匹配。结构化修复后相同链命中站点。
- 全量4178 passed / 9 skipped（231.97秒）；JUnit在`dist/verification/site-query-full-final.xml`。
  第一次全量4失败：1条旧schema断言、节点锁待更新、2条越界夹具被新真实过滤提前过滤；
  已同步schema/制品，越界反例显式模拟上游不守过滤，仍必须拒绝。没有削弱权限校验。
- 相关专项207通过；后续新参数/消费者范围/代理反例另有33和48项通过。
- 真Java/Spring AI/Gateway整链19场景通过；站点场景实际使用数组工具调用，快照输入逐项核对。
  第二轮严格CPMS过滤夹具整链也通过，报告`dist/verification/site-java-final/report.json`。
- 真实模型相同问法对照：旧协议3次均为address字符串，新协议3次均为address_terms数组。
  首轮直接python误加载canonical包，已用uv run核对模块路径后重测；前一组归入旧协议，不冒充新代码。
- Maven离线package成功（无JUnit不计测试）；Gateway镜像、wheel/sdist构建通过；
  verify-aiops-gateway一次性8项、Ruff/格式与git diff检查通过。

覆盖率专项133项通过：read_site_tool 4/4语句，read_site_pages 30/32语句、20/22分支；
参数校验35/42语句、27/34分支。查询函数近似圈复杂度13；保留硬词数/分页上限。
变异把数组重新拼成单字符串后，真实Gateway原问法回归重新失败，证明测试覆盖真实断点。

## 未完成范围

尚未部署、远端CI待验证；必须按[部署QA](../agents/operator-cpms/site-query-real-path-qa.md)同步
节点jar/声明/Gateway，并验证原Dify与H5。未完成业务验收，不声称长期模型稳定性或全地名召回。
关键词候选搜索不是地理距离或行政层级交集；不解决只有拼音地址时的任意中文城市查询。
