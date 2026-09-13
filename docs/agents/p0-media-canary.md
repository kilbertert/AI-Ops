# P0 客服媒体闭环真实 canary 执行手册（#173 / #175）

> 状态：2026-09-11 修订。**KB 栈已随 PR #181 全栈迁移至移动云 36 并实测
> 活通**（网关 `/health` 200、kb-service `/healthz` 200、kb→RAGFlow 搜索
> 实响、媒体签名配置生效）。本手册按 36 实况修订，剩余人工决策点见 §0。
> 读者：执行本 canary 的 engineer/agent（一次跑完），以及业务方确认人工
> 决策点。前置阅读：`docs/agents/kb-service-test-env.md`（120 侧环境与
> canary 纪律；36 侧迁移记录见 `docs/validation.md` §移动云 36）。
> 敏感边界：本文不含任何密钥/密码；服务令牌只存在于部署配置。

## 0. 人工决策点（2026-09-11 执行状态）

1. ✅ **36 公网切流已落地**（2026-09-11）：`api.qumall.qushiyun.com/v1/*` →
   120 nginx → 36:8789 → 36 网关；C3-C6 已按公网域名执行。
2. ✅ **kb-service 图片透传端点已补**（部署 36 并实测真实字节；源码入库
   Playground 沙箱仓库）。
3. ✅ **C4 视频回源已通过**（2026-09-12）：新鲜 QA 返回有效 MP4；公网整段
   GET 为 `200`，`Range: bytes=0-1023` 为 `206`，超范围为 `416`，伪造签名为
   `403`。为保证浏览器 Range 合同，120 `/v1/` 与 36 `8789` 两层 Nginx 均显式
   转发 `Range`，配置变更已 `nginx -t` 并平滑 reload。
4. ~~KB 栈重启~~（已随 36 迁移解决）：36 上 kb-service(9380) + RAGFlow
   (19380) + AI-Ops 网关(8788) 均已运行且开机自启；`AIOPS_GATEWAY_KB_
   SERVICE_BASE_URL` 与媒体签名密钥已配置。
   注意：36 与 120/124 内网不互通（诊断数据面在 36 不可达属预期）；
   诊断线继续由 120 网关承载，不在本 canary 范围。

> 环境口径：本文早期 C1-C7 记录使用 `api.qumall.qushiyun.com` 的历史 36 canary 入口；
> 当前正式分流为 95 `api.qumall.qushiyun.com`、41 `api.mall.qushiyun.com`。在 41 复跑时必须使用
> `api.mall.qushiyun.com` 及 41 会话，不能把 95 请求导入 41。

## 1. 链路（按 §0.1 决策点二选一）

**A. 切流后（公网）**：
```text
https://api.mall.qushiyun.com/v1/*   (41 入口 → 41 网关 → 36 KB 隧道)
    │  注入服务身份 Authorization；转发 third-session（全小写连字符！）
```
历史 36 canary 记录使用 `https://api.qumall.qushiyun.com/v1/*`，该域名当前已恢复为 95 环境，
不得继续作为 41 验收入口。
请求头（所有 /v1 调用）：
```http
third-session: <有效 thirdSession>    # 全小写；X-Third-Session 会被 nginx 丢弃
tenant-id: 2019588094906601472        # 既有联调租户
```

**B. 切流前（36 本机/SSH）**：直接调 `http://127.0.0.1:8788/v1/*`，
`Authorization: Bearer <36 部署配置中的服务令牌>` + `third-session`。
接口语义与公网路径等价；差别只在入口（反代注入的服务身份）。

管理端点（/v1/agents、/v1/agent-metrics）鉴权走服务身份 + agent 角色，
canary 用部署配置中的 service token。

## 2. canary 租户与数据

- 租户 ID 固定 **`aiops-canary`**（kb-service 懒注册映射删不掉，必须复用，
  见 kb-service-test-env.md §2）。
- 知识库：`chunk_method=picture` 新建临时库 `canary-media-<日期>`；
  素材：`docs/知识库材料/`（gitignored）——`宣传.docx`（含图）、
  `新加坡无人电动巴士.mp4`（82MB）、`重卡充电.mp4`（370MB，Range 的好对象）。
- 上传后确认解析 `run=DONE`（RAGFlow 文档列表）；canary 租户配的模型需含
  VISION 能力，否则视频文档解析失败（≠检索不可用）。

## 3. 执行序列（apifox/curl，逐步留证）

每一步记录：请求/响应全文（脱敏后）、时间戳、commit（网关版本）、结论。

### C1 管理面：创建 → 发布客服智能体

```http
POST /v1/agents
{ "name": "canary-客服", "agent_type": "customer", "prompt": "<业务说明>",
  "knowledge_base_ids": ["<kb_id>"], "model": "<allowlist 模型>",
  "output_contract": "blocks-v1", "opening_questions": [], "quick_commands": [] }

POST /v1/agents/{agent_id}/publish   {"expected_revision": 1}
```
预期：创建 201；发布 200 版本 1。**校验真实生效**：发布一个绑定不存在
kb 的草稿应被拒且 message 含"不存在"；绑定解析中文档时 message 含"解析中"。

### C2 草稿调试（发布前后各一次）

```http
POST /v1/agents/{agent_id}/debug-run   {"question": "充电桩怎么操作"}
```
预期：发布前草稿 200，返回 blocks[] + retrieval_status + `debug:true` +
`agent_version=…#draft-rN`；发布后（已发布态）409。

### C3 客服 QA：提问 → blocks → 媒体

```http
POST /v1/assistant/questions
{ "question": "新加坡无人电动巴士怎么充电？", "conversation_id": "<C5 创建>" }
→ 202 {"type":"qa","qa_id":"qa_…"}   轮询 GET /v1/assistant/questions/{qa_id}
```
预期：`result.blocks[]` 含 text + video（对含视频知识库）或 image，引用块
reference 带文档名；`retrieval_status=found`；媒体块 `media.url` 形如
`/v1/media/media_<id>.<hmac>`。

### C4 媒体消费（Range 关键）

```bash
# 图片：直接 GET，200 + image/png|jpeg|webp
curl -sI "https://api.qumall.qushiyun.com<media.url>"
# 视频：整段 200；分段 206（Range 头）
curl -s -o /dev/null -D - "https://api.qumall.qushiyun.com<video_url>" \
  -H "Range: bytes=0-1023"     # 预期 206 + Content-Range: bytes 0-1023/…
curl -s -o /dev/null -w '%{http_code}\n' "https://api.qumall.qushiyun.com<video_url>" \
  -H "Range: bytes=999999999999-"   # 预期 416
```
预期：图片/视频在无 Authorization 头下可取（签名 URL 即凭证）；
伪造 id → 403 no-store；未配置媒体代理 → 404。

### C5 多轮会话

```http
POST /v1/conversations   {"agent_version_key": "<agent_id>#v1"}
→ 201 conversation_id；C3 提问带 conversation_id；追问"还有别的视频吗"再答一轮
```
预期：会话上下文生效（第二轮引用第一轮话题不重述）；同会话并发提问
409 CONVERSATION_BUSY；删除会话后 GET 404。

### C6 降级与越权（一次配齐）

- 无命中：问一个知识库没有的问题 → `retrieval_status=not_found`，文本仍交付。
- kb 不可用：在 36 上临时 `systemctl stop kb-service` → 提问 → 降级
  `unavailable` 文本仍交付 → **立即恢复**（`systemctl start kb-service`），
  并确认 `/healthz` 回到 200。
- 媒体失败：删除知识库文档（或等 600s TTL 过期）→ 重取 media.url →
  403，且已完成回答的文本/引用不受影响。
- 跨租户：另一个租户头调用同一 media.url → 预期仍可取？**否**——签名 URL
  与租户头无关（URL 即凭证）；跨租户验证点在 QA/管理面（C1 的 kb 校验、
  C3 的会话隔离），媒体面验证 TTL/失效即可。

### C7 监控面（T7）

```http
GET /v1/agent-metrics/summary?window_hours=24
GET /v1/agent-metrics/runs?route_type=qa
```
预期：C2-C5 的运行计数与结果吻合；行内无问题原文（抽查最近一条 qa 行）。

## 4. 清理（canary 纪律）

1. `DELETE /kb/knowledge-bases/{canary_kb}`（临时库验收完即删；
   租户映射 `aiops-canary` 复用不删）。
2. AI-Ops 侧：`POST /v1/agents/{id}/disable` → 删除草稿（已发布的按仓库
   惯例保留状态不可删；canary 智能体如无保留价值，停用即可）。
3. 会话：删除本次创建的 conversation；监控行按 30 天保留自然过期。
4. 证据：按 `~/Playground/experiments/kb-service-design/qa-evidence-*.md`
   的"步骤/结果/根因/残留"表格式落到 `docs/validation.md` 引用位置，
   并在 #173/#175 issue 回填结论。

## 5. 验收判定（对应 issue 勾选）

| #173 验收项 | 判定证据 |
|---|---|
| 前端只调 BFF 域名、不提交 agent_id/tenant/token | 反代链路事实 + API 合同（已由接口级测试覆盖），canary 记录 C3 实际载荷 |
| 三类结果按入口合同渲染 | C3（qa）+ 既有 FAQ/diagnosis 线（9-08 已实测）|
| 图片块按 MIME 渲染 | C4 图片 200 |
| 视频块可播放、Range | C4 206/416 |
| 多轮 + 检索状态/引用 | C5、C3 |
| 媒体失效文本引用保留 | C6 |
| 并发忙碌/停止 | C5（409）|
| 跨租户拒绝 | C6 + 既有租户隔离测试 |

#175 的 acceptance.feature/QA 计划在 `acceptance.feature`（P0-E2E-REAL
场景）与 `qa-plan.md`；本手册是它们的执行载体。**任何一步失败：记录
BLOCKED + 原因，不勾验收项。**
