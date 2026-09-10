# kb-service 测试环境与协议样例（面向 #170 及后续票）

> 状态：当前有效，2026-09-10。读者：#170 实现者（Codex/AFK）、#171/#173 联调负责人。
> 来源：2026-09-08~09 全链路浏览器验收 + RAGFlow v0.27.1 源码级核实（部署版本与
> `~/Playground/.ragflow-study` 研究副本一致，适配层 md5 已比对）。
> 敏感边界：本文只含公司内部域名与协议事实，**不含任何密钥/密码**；RAGFlow 原生
> API 仅监听 `127.0.0.1`，不对 AI-Ops 或前端开放。

## 0. 一图看懂：kb-service 在哪、谁调它

```text
前端/管理后台
    │  只带公司登录态（admin 域名走公司 OAuth token）
    ▼
admin.qumall.qushiyun.com（120.55.45.59 Nginx）
    │  /kb/** location
    ▼
cloud-gateway（公司 Java 网关，120 本机 9999）
    │  Nacos dynamic_routes 里 id=kb 路由（RewritePath 把 /kb 写回）
    │  AdminProxyHeadFilter/ApiProxyHeadFilter 从 token 解出 tenant-id/user-id 注入
    ▼
kb-service（适配层，systemd 单元，127.0.0.1:9380，仅回环）
    │  每请求: tenant-id → tenancy.db 映射 → ragflow token
    ▼
RAGFlow v0.27.1（5 容器 compose 项目 ragflow-kb，API 127.0.0.1:19380，仅回环）
```

AI-Ops 的位置：**AI-Ops 不在前端这条链上**。`KbServiceClient`
（src/aiops_diagnostics/knowledge_retrieval.py:386）从部署配置拿到 kb-service
base_url 后直调 `/kb/knowledge-bases/{kb_id}/search`。AI-Ops 网关生产实例与
kb-service 同在 120（见 docs/validation.md §服务迁移），内网可达。

## 1. 当前可用状态（重要，2026-09-10 实测）

**RAGFlow 5 容器与 kb-service 于 2026-09-09 15:13 被人为停机**（root 操作
`docker stop ragflow-kb-*` + `systemctl stop kb-service`，停机前打了完整回滚
快照 `/opt/ragflow-kb/rollback-20260909_151325/`；120 内存 26Gi/30Gi 紧张是背景
因素）。公网 `https://admin.qumall.qushiyun.com/kb/healthz` 当前返回 **502**
（网关活着、下游 kb-service 没起）。

对 #170 的含义：

- **不需要活栈也能开工**。T1（#168）已确立 mock-first 模式：
  `KnowledgeSearchClient` 是 Protocol，测试注入 fake 即可覆盖全部验收
  （命中图片/命中视频/无命中/不可用/越权/非法类型，见
  tests/test_knowledge_retrieval.py）。#170 同理先接 qa 路径 + `blocks[]`。
- 真实栈恢复是一个**人工决策点**：重启命令与回滚路径都在
  `/opt/ragflow-kb/kb-service/OPS.md`（120 上）。重启前先和昨天停机的人对齐
  （120 有活跃 root 会话；124 侧 KB 栈此前已停，迁移快照在
  `/opt/ragflow-kb/migration-20260908_193141`）。**Agent 不要擅自重启生产栈**。

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

## 6. 剩余人工决策点（不是 #170 的前置）

1. **重启 120 KB 栈**（或决定迁独立节点，OPS.md §6 提到内存长期偏紧）——
   与昨天停机的 root 操作者确认后执行 OPS.md §1.1/§4.3 命令。
2. **kb-service 补图片透传端点**——代码在 120 + Playground 双副本，改动需
   同步两处并 `systemctl restart kb-service`；属于 kb-service 仓库的改动，
   不属于 AI-Ops PR。
3. **#171/#173 的外部依赖**（Java BFF 仓库访问、qumall-admin checkout/菜单权限/
   测试账号）——需要业务方协调，与本测试环境无关，阻塞的是管理后台与 BFF 票，
   不是 #170。#170 的"Java BFF 透传"验收项当前以同域 `/v1/*` 反代（120 nginx）
   满足，用公网 `api.qumall.qushiyun.com` + windows-rl1 验收机即可覆盖
   （2026-09-08 已实测 FAQ 200 / 自由提问 202→completed）。
