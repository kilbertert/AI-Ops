# kb-service 测试环境与协议样例（面向 #170 及后续票）

> 状态：当前有效，2026-09-11 修订（KB 栈已迁移动云 36 并完成公网切流，见 §1）。
> 读者：#170 实现者（Codex/AFK）、#171/#173 联调负责人。
> 来源：2026-09-08~09 全链路浏览器验收 + RAGFlow v0.27.1 源码级核实（部署版本与
> `~/Playground/.ragflow-study` 研究副本一致，适配层 md5 已比对）。
> 敏感边界：本文只含公司内部域名与协议事实，**不含任何密钥/密码**；RAGFlow 原生
> API 仅监听 `127.0.0.1`，不对 AI-Ops 或前端开放。

## 0. 一图看懂：kb-service 在哪、谁调它

```text
【主链路（2026-09-11 切流后）】
浏览器/前端
    │  third-session（全小写连字符）
    ▼
api.qumall.qushiyun.com（120.55.45.59 Nginx，TLS/域名不动）
    │  /v1/* 注入服务身份 Authorization → 反代 36.156.159.175:8789（仅收 120）
    ▼
AI-Ops 网关（36 本机 127.0.0.1:8788，systemd aiops-gateway.service）
    │  KbServiceClient 直调 http://127.0.0.1:9380
    ▼
kb-service（36，systemd 单元，127.0.0.1:9380，仅回环）
    │  tenant-id → tenancy.db 映射 → ragflow token
    ▼
RAGFlow v0.27.1（36，5 容器 compose 项目 ragflow-kb，API 127.0.0.1:19380，仅回环）

【管理后台链路（历史拓扑，公司侧待修）】
admin.qumall.qushiyun.com（120 Nginx）/kb/** → cloud-gateway(120:9999, Nacos id=kb)
    → 仍指向 124.243.178.156:9380（已停机的旧主机）——需公司侧把 /kb/ 改指 36:9380
```

AI-Ops 的位置：QA/RAG 主链路上，AI-Ops 网关与 kb-service 同在 36，内网回环直连。
诊断线（订单诊断依赖 MySQL/TDengine 数据面）仍由 120 网关承载——36 与 120/124
内网不互通（同段不同 VPC），36 通过 120 的 SSH 隧道访问会话 Redis/UPMS MySQL/UPMS API
（systemd 单元 `aiops-session-redis-tunnel`，127.0.0.1:26379/23306/25999，
restricted authorized_keys + permitopen 白名单）。

## 1. 当前可用状态（2026-09-11 修订）

**KB 栈已随 PR #181 全栈迁移至移动云 36（36.156.159.175，SSH 别名 `yidong-36`）并实测活通**：
网关 `/health` 200（127.0.0.1:8788）、kb-service `/healthz` 200（127.0.0.1:9380）、
kb→RAGFlow 搜索实响、媒体签名配置生效。2026-09-11 公网切流完成：`api.qumall.qushiyun.com/v1/*`
经 120 nginx 反代 36:8789（TLS 复用 120 证书，仅收 120 IP）→ 36 网关；120 侧网关与
回滚快照原样保留（回滚 = 120 rewrite 配置改回 `proxy_pass http://127.0.0.1:8788` 一行）。
历史停机记录（2026-09-09 人为停机 120 栈、回滚快照 `/opt/ragflow-kb/rollback-20260909_151325/`）
已被迁移取代。

### 36 环境关键事实

- **模型配置**：kb-service 懒注册只配 chat/embedding/rerank；**视频/图片解析需租户
  VISION 模型**（RAGFlow `tenant.img2txt_id`，如 `qwen-vl-max@maas@Tongyi-Qianwen`）。
  配法：`PUT /api/v1/providers/Tongyi-Qianwen/instances/maas` 的 model_info 加
  `image2text` 项 → `PATCH /api/v1/models/default`（model_type=vision）。缺它时
  picture 解析报 "No default vision model is set"，run=DONE 但 0 chunk。
- **RAGFlow 视频解析上限 128MB**（`File size exceeds`），更大视频不产出 chunk。
- **图片透传端点已补**（2026-09-11）：`GET /kb/documents/images/{image_id}`
  （app.py + kb_adapter.download_image，参照 download_document 模式；RAGFlow 业务层
  "不存在"的 HTTP 200 + JSON 映射为 404）。已部署 36 并实测真实字节；120 侧未部署。
  kb-service 无独立 git 仓库：源码试验记录在 `~/Playground/experiments/kb-service-design/`
  （已提交），部署副本（36 `/opt/ragflow-kb/kb-service/`）以 md5 双副本纪律同步。
- **部署代码版本**：36 网关当前为 #185 基线 + #186 answer 归一 hotfix；**#187/#188/#190
  （媒体回源业务错误映射、Range 本地切片、瞬时 not found 重试）已合并 main 但待部署 36**，
  部署后需重验 C4（视频整段/Range）。
- **供应商 key 状态**（Codex agent 链）：alibaba-maas 被供应商封锁（API-key is blocked，
  需业务方轮换）；glm-ark 月配额 2026-09-21 重置；psydo 余额不足。canary 期间以
  `canary-dashscope` provider（DashScope key + `qwen3.8-max-0902`，wire_api=responses）
  临时顶替——注意该 provider 的 `wire_api=chat` 已被 Codex CLI 弃用，且 DashScope
  compatible-mode 对 tool 往返仅在 `qwen3.8-max-*` 系列验证可用。
- **联调身份**：C 端 thirdSession 取自公司 Redis `app:3rd_session:*` 键（120:6379，
  ACL 用户 `aiops_third_session` 只读；有效会话由业务方/运维提供，不落文档）。

## 2. 联调鉴权：两条路线

| 路线 | 场景 | 身份头 |
|---|---|---|
| A 公司网关（生产） | 经 admin 域名 | `Authorization: Bearer <公司OAuth token>`，网关注入 `tenant-id` |
| B 联调直连 | AI-Ops/测试直调 kb-service | 手动 `tenant-id: <任意稳定标识>`（可选 `user-id`） |

AI-Ops 集成测试走路线 B。**首次出现的 tenant-id 会触发懒注册**：kb-service
自动在 RAGFlow 注册新租户 + 配默认模型 + 建 tenancy.db 映射记录
（kb-service.env 需有效的 MAAS_API_KEY，2026-09-09 已配好）。这条映射记录
**无法通过 API 删除**——canary 请固定复用同一个测试租户 ID（建议
`aiops-canary`），不要每次随机，避免 RAGFlow 用户表积累垃圾租户。

## 3. 检索协议（AI-Ops 视角的唯一接口）

`KbServiceClient.search` 按知识库逐个调用 kb-service，多库结果在客户端侧合并：

```http
POST /kb/knowledge-bases/{kb_id}/search
Content-Type: application/json
tenant-id: <测试租户标识>

{"question": "充电桩怎么操作", "top_k": 5}
```

kb-service 原样转发到 RAGFlow `POST /api/v1/datasets/{dataset_id}/search`，
返回（HTTP 200）：

```json
{
  "code": 0,
  "data": {
    "chunks": [
      {
        "chunk_id": "…",
        "content_with_weight": "命中分段正文（脱敏前）",
        "doc_id": "…",
        "docnm_kwd": "文档名如 操作手册.pdf",
        "kb_id": "…",
        "image_id": "…或空串",
        "doc_type_kwd": "text | image | video | table",
        "similarity": 0.83,
        "vector_similarity": 0.9,
        "term_similarity": 0.6,
        "important_kwd": ["…"],
        "positions": [],
        "total": 1
      }
    ],
    "total": 1,
    "labels": []
  }
}
```

字段事实（RAGFlow v0.27.1 `rag/nlp/search.py` 投影 + `chunk_api.py:470` key
mapping，源码核实）：

- `similarity` 是 RAGFlow `/retrieval` 端点的叫法；`/datasets/{id}/search`
  走 `search_datasets` 服务，投影相同、字段名一致（`kb_id` 不重命名为
  `dataset_id`——那是 `/retrieval` 端点的行为）。`KbServiceClient` 已兼容两者。
- `image_id`：图片型分段（`doc_type_kwd=image`）携带；文本分段为空串。
- 空命中返回 `"chunks": [], "total": 0`（不报错）。
- kb-service 上游异常 → HTTP 502 `{"error": "upstream_error", "message": "…"}`；
  AI-Ops 侧 `KnowledgeSearchUnavailable` → `retrieval_status=unavailable`，
  文本结果仍可交付（T1 语义）。
- `top_k` 在 RAGFlow v0.27.1 已弃用为 `knn_top_k` 别名但**仍然接受**；kb-service
  契约保持 `top_k`，无需改。

## 4. 媒体资源读取路径（#170 blocks[] 的数据面）

T1 的媒体块只是**不透明 ID + 短时签名授权**（`MediaResourceSigner`/
`MediaProxy`，内部 `knowledge_retrieval.py`）。真实字节从哪来，本次已核实：

### 4.1 图片（PNG/JPEG/WebP）

- RAGFlow 原生端点：`GET /api/v1/documents/images/{image_id}`
  （`document_api.py:1836`，`image_id` 格式为 `{dataset_id}-{对象key}`，仅按
  第一个连字符分割；Content-Type 按扩展名/魔数探测，PNG/JPEG/GIF/WebP/BMP）。
- **kb-service 适配层目前没有透传这个端点**——这是 #170 真实验收前需要补的
  一个小端点（约 15 行：`GET /kb/documents/images/{image_id}` 带 tenant 校验，
  参照现有 `download_document` 模式）。适配层源码在
  `/opt/ragflow-kb/kb-service/app.py`（120）与
  `~/Playground/experiments/kb-service-design/`（本地副本，md5 一致）。
- 备选：图片若按文档上传（chunk 挂 `image_id`），也可走文档 download 端点取
  原文件，但**按 chunk 的 image_id 直取才是分段级授权**，媒体代理应采用前者。

### 4.2 视频（MP4/WebM）

- 适配层**已有**：`GET /kb/knowledge-bases/{kb}/documents/{doc}/download`
  返回原始字节流（`Content-Disposition` 带原文件名）。
- RAGFlow 视频分段的产生：`chunk_method=picture` 的知识库上传视频文件时
  （`rag/app/picture.py:65`），VISION 模型对整段视频生成一段文字描述，
  `doc_type_kwd="video"`，chunk 挂在文档上。**视频分段本身不切片**——前端播放
  靠 AI-Ops 媒体代理对 download 字节流实现 HTTP Range（T1 `parse_byte_range`
  已实现并测试 suffix/open-ended/多 range 拒绝）。
- 注意：视频解析强依赖租户配了可用的 VISION 模型（默认走 MAAS DashScope），
  canary 租户配模型时需含 vision 能力，否则视频文档解析失败（解析失败≠检索
  不可用，该文档不会被检索命中）。

### 4.3 canary 素材

业务方 2026-09-10 提供了联调素材（本机 `docs/知识库材料/`，gitignored，不入库）：
`宣传.docx`（5MB，含图）、`新加坡无人电动巴士.mp4`（82MB）、`重卡充电.mp4`
（370MB）。370MB 视频对媒体代理的 Range 实现是好测试对象。上传后在
`chunk_method=picture` 库里跑通"命中视频 → blocks[] → Range 播放"链路。

## 5. canary 纪律（照抄既有模板）

- 建**临时**知识库验证，验收完即删（DELETE /kb/knowledge-bases/{kb}）；9-08/9-09
  两轮验收都是这么做的，无残留。
- 租户映射记录删不掉 → 固定复用 `aiops-canary` 租户 ID（§2）。
- 凭据/密码/token 不写入任何文档或 PR；密码只存 120 的
  `/opt/ragflow-kb/kb-service/kb-service.env`（600, root）与本机私有配置。
- 验收记录格式参照 `~/Playground/experiments/kb-service-design/qa-evidence-*.md`
  的"步骤/结果/根因/残留"表。

## 6. 剩余人工决策点（2026-09-11 修订）

1. ~~重启 120 KB 栈~~ **已被 36 迁移取代**（PR #181，经业务方授权；120 栈与回滚
   快照原样保留）。
2. ~~kb-service 补图片透传端点~~ **已完成**（2026-09-11 部署 36 并实测；源码记录在
   Playground 沙箱仓库，见 §1）。
3. **公司侧待办（非 AI-Ops 仓库）**：admin 域名 120 nginx 的 `/kb/` location 仍指向
   已停的 124.243.178.156:9380，需改指 36（kb-service 9380 仅回环，需经 36 本机
   nginx 或隧道暴露给公司网关，方案待公司侧定）；UPMS 建 `ROLE_AGENT_ADMIN` 角色族
   并授权管理账号（管理面 HTTP 化的前提，当前管理操作走 on-box 方式）；业务方轮换
   被封锁的 alibaba-maas key（canary-dashscope 为临时 provider）。
