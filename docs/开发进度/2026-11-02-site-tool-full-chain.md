# 站点工具整链修复（参数 → 过滤器 → 证据 → 答复，以及节点与回调）

**日期**：2026-11-02　**分支**：`fix/callback-site-route`　**基线**：`2a59a18e`

## 目标

Codex 复核拒绝了站点候选的合并部署，列出五个已复现问题。本里程碑把它们修到「整条链
可执行」，而不是逐个消红。

## 完成内容

| 问题 | 修法 | 位置 |
|---|---|---|
| 参数判据放过重排/重组 | `question_span`：去掉空白后必须是原文的**连续子串**，返回原文那一段；工具参数与最终答复共用 | `operator_conversation.question_span` |
| 参数放行、最终答复仍 `OPERATOR_SITE_SEARCH_MISMATCH` | `validate()` 返回订正后的答复；`_sites_from_evidence`/`SiteKeywords.address` 用同一地址含义比对 | `operator_cpms` |
| nginx 模板无条件硬编码 Lua | 模板只留 `${AIOPS_NODE_LUA_DIRECTIVES}`，由 `--lua-directives` 注入；41 的两行作为该环境受控配置 | `deploy/node-runtime/*`、`tools/content/render_node_callback.py` |
| 回调路径正则放宽超出声明 | 按每条 `path_template` 生成完整正则（订单路径必须有单号、站点分页无尾段）；正则加引号避免 `{1,80}` 被分词 | 同上 |
| Java 测试只搜源码文字 | 新增 `CpmsToolAcceptance#authorizationBodyIsTheGatewayContractShape`：真实 `CpmsGatewayAuthorization` 发请求、本地服务器捕获请求体 | `tools/experiments/m2_close/m3node/` |

复核清单之外，**端到端复现**又暴露两处断点（只测「授权 200」看不见）：

- 网关代理遍历全部声明的工具去匹配请求目标，无订单回合没有订单号 →
  `ToolDeclarationError` → 站点工具每次取数 500。
- 网关代理与节点读取都按**订单行**校验站点页（`orderNo`/`siteId`），站点行没有这些字段；
  节点 `Grant` 还要求非空 `site_ids`，而空集合正是「确实没有可见站点」的合法答复。

## 验证

- 全量：`3838 passed / 9 skipped`（唯一红项是按规程要等合并后才写的 `source_commit`）。
- 真实 nginx：渲染器生成的配置启动并**持续存活**，声明里的每个工具都能到达上游；
  `chSite/pageBOGUS`、`chSite/page/ORDER-1`、`chOrderInfo/getOrderInfoDetail`（缺单号）全部 404。
- Java：`CPMS_TOOL_ACCEPTANCE PASS assertions=183`（含站点授权体、站点页证据整链）。
- 制品：节点 jar 两次干净构建同摘要 `00b49b69…`（106302423 字节）；Gateway 上下文摘要
  `94b963ec…`（`--update-lock` 写入，`context_digest --check` match）。

## 已知缺口

- 生产侧验收未做：本轮只证明离线整链与制品身份；账单、知识、页面生命周期仍未验收。
- `node.source_commit` 在合并前无法写成真实 SHA（浅克隆外无该提交），按既有规程留待合并修订。
