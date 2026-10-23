## 2026-10-23 · 库被删：四处引用改指，两份新素材入库

**起因**：用户要导入 `用户端(1).docx` 与 TRENDPOWER 视频模块表。只读核对目标库时发现
`8701c742…`（「41-客服知识库」，12 文档）已不存在。

**事故时间线**（RAGFlow 日志）：18:06:35 同一租户管理员账号从本机回环登录 →
18:07:13 `DELETE /api/v1/datasets` 200 → 18:07:14 Infinity 表被 drop。
**谁点的没查出来**；我这轮只做过 GET 与一次 search。

**同一个原因四种形状**：kb-service 502 `code=102`、`kb-canary-2` 502、库级 id 404、
tenant 级合并形态 200 但静默少结果。靠报错判断发生什么会先被这四种形状骗一遍。

**决定改指 `41(1)` 而不重建**（用户当场决策）：MinIO 桶还在，但 10-22 已确认 12 个文档里
5 张 PNG 内容损坏且无原件 —— 重建 = 7 个真文档 + 5 个坏图。见 ADR-0102。

**改了四处**：`env-41.toml` 两个 agent、`production.env` 的 `kb-canary-2`、Dify dataset
「41」（只改 `external_knowledge_id`）、网关两个已发布 agent（`fork_draft → update →
publish`，v17 / v4）。

**踩到一个**：第一次 publish 没注入 `KbBindingResolver` → `knowledge-base validation is
unavailable`，而 agent 已被切成 **draft**（那一瞬间线上是"已发布版本还在、当前配置是草稿"）。
补上解析器重跑才成功。直接 UPDATE 库会绕过模型白名单与库活性两道校验，所以只能走生产代码。

**没改历史**：`agent_versions` v9–v16 仍写旧库 id —— 审计事实，不是待修状态。

**两份素材**：`用户端(1).docx` 原样上传（20 段）；xlsx **先转带字段标签的 Markdown**
（11.8 KB，14 段）再上传 —— 三个 sheet 的表头在合并单元格里，直传会切出孤立数字。

**判据**（公网入口）：`kb-canary-2` / tenant 级 / 库级 三条均 200 且有记录；
`kb-canary` 0 条是存量（另一租户，只装视频分段），与本轮无关。
**视频链**：media 签发 → 整段 200 / 82,348,365 字节 / ISO MP4；Range 206；伪造签名 403。

**缺口**：删除来源未查明；`41(1)` 少 5 张宣传图；Dify app 的人工提问未跑。

**交付**：本分支（清单 + 文档 + ADR-0102 + 验收记录）。
