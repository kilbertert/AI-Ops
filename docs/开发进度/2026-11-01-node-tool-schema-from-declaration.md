# 节点侧工具 schema 改从声明生成

日期：2026-11-01。分支 `fix/node-tool-schema-from-declaration`，基线 `154faf39`。

## 一句话

节点对每个 CPMS 工具都用同一个**空参数** schema，而声明里 `cpms_site_search` 有
`name`/`address`；`verifyTools` 比 sha256 判漂移，整轮起不来。#1296 部署后失败点从
`operator_failed` 变成 `operator_node_failed` 就是这个。

## 提交

- `OperatorTools.java`（Entry 增加 modelInputs + 新增 inputSchema(name)）
- `CpmsReadTool.java`（改用声明的 schema）
- `tools/experiments/m2_close/m3node/ToolSchemaParity.java`（新增验收件）
- `docs/validation/2026-11-01-node-tool-schema-from-declaration.md`（证据全文）

## 验证

Java 侧对**实际构造出的 CpmsReadTool** 自算摘要，与 Python 声明算出的期望值比对：
3/3 PASS。把 `CpmsReadTool` 改回空参数常量后 `cpms_site_search` 变 FAIL。

第一版验收读的是 `OperatorTools.inputSchema()`（声明侧字符串），回退修复后仍 PASS——
判据没读被测路径，已重写。这与 `verifyTools` 是同一条教训。

## 已知缺口与下一步

- **未部署**：节点是 Docker 容器，新 jar 需要按节点发版流程构建镜像并替换。
- 部署后再跑一次管家端站点问题，确认 `agent_runs.state=succeeded`。
- 消费者端的形态问题（`CHATFLOW_FACT_UNKNOWN`、偶发非 JSON）仍未修。
