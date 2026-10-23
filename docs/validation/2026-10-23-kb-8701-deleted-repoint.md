# 41 知识库被删后的改指，与两份新素材入库（2026-10-23）

**范围**：36 的 RAGFlow / kb-service、41 的网关与 Dify（dataset / app / env）。
**性质**：真实生产数据与生产配置变更，含一次重启。

## 一、先说发现的事故

本片开工时的原任务是「把两份新文档导进知识库」。在只读核对目标库时发现
`8701c742b28111f18637d95f7710e3a3`（「41-客服知识库」，12 文档）**已经不存在**。

```
18:06:35  POST /api/v1/auth/login    200   ← 559af59…，127.0.0.1（RAGFlow 控制台）
18:07:13  DELETE /api/v1/datasets    200
18:07:14  INFINITY dropped table ragflow_559af59…_8701c742…
```

**没有确认是谁点的。** 能确认的只有：真实浏览器会话、同一租户的管理员账号、本机回环。
我的会话在本轮只做过 GET 与一次 `search`，没有任何 DELETE。

**删除后同一个原因呈现四种形状**（这才是排查里最容易走偏的地方）：

| 入口 | 形状 |
|---|---|
| kb-service 文档列表 | `502 upstream_error` / `code=102 You don't own the dataset` |
| 网关 `kb-canary-2` | **502** |
| 网关 `…:8701c742…`（库级 id） | **404** |
| 网关 tenant 级合并形态 | 200，静默少了这个库的结果 |

## 二、处置（用户当场决策：改指 `41(1)`）

不进 MinIO 重建，理由与代价记录在 [ADR-0110](../../adr/0110-repoint-on-deletion-instead-of-rebuild.md)。

| 引用 | 改法 | 回读 |
|---|---|---|
| `AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS` | `kb-canary-2` → `5c9313bc…` | 重启后 200 + 记录 |
| Dify dataset「41」`66d3481c…` | 只改 `external_knowledge_id` | 列表回读已变，`permission` 与 `retrieval_model` 原值不变 |
| `env-41.toml` 两个 agent | 清单改 id | 见下 |
| 网关 `canary-客服` / `canary-宣传案例` | `fork_draft → update → publish` | v17 / v4，状态 `published` |

**一次失败且必须记住的**：第一次 publish 没注入 `KbBindingResolver`，报
`knowledge-base validation is unavailable`，而 agent **已经被切成 draft** ——
线上那一瞬间是「已发布版本还在、当前配置是草稿」。补上解析器后重跑才发布成功。
直接 `UPDATE agents.config_json` 会绕过这两道校验，所以这条路径只能走生产代码。

**没改历史**：`agent_versions` 的 v9–v16 仍写着 `8701c742…`。那是审计事实。

## 三、两份新素材

| 文件 | 处理 | 结果 |
|---|---|---|
| `用户端(1).docx`（4.4 MB，28 个 Q/A） | 原样上传，`naive` 解析 | `run=DONE`，20 段 |
| `TRENDPOWER_EV_Charging_Platform_Video_Modules.xlsx` | **先转成带字段标签的 Markdown**（11.8 KB）再上传 | `run=DONE`，14 段 |

xlsx 不直传：三个 sheet 的表头在合并单元格里，直接交给解析器会切出一堆孤立数字。
转换用 `-` 标签逐字段展开，检索实测能命中（`Nigerians` 相似度 0.863、`OCPP 协议` 0.872）。

## 四、判据（全部在公网入口 `api.mall.qushiyun.com` 上跑）

```
kb-canary-2                                       → 200，1 条（命中 用户端(1).docx）
tenant-1942105476598861824                        → 200，1 条
tenant-1942105476598861824:5c9313bc…              → 200，1 条
kb-canary                                         → 200，0 条   ← 见下
```

`kb-canary` 的 0 条是**存量状态**：它是另一个租户（`1783…792`）的库，只装了
`canary-media-0911` 的视频分段，对「充电枪」本来就没有命中。与本轮无关，不改。

**视频链路单独验**（这是用户问题的第二半）：

```
检索「新加坡无人电动巴士」→ record 带 media[0]，kind=video, video/mp4
  整段 GET         → 200，82,348,365 字节，ISO Media MP4
  Range 0-1023     → 206，1024 字节
  伪造签名         → 403
```

## 五、RAGFlow 里删除没清 MinIO

`/data/8701c742b28111f18637d95f7710e3a3` 桶仍在，12 个对象目录都在。
**但"桶在"不等于"数据在"**：2026-10-22 已确认其中 5 张 PNG 的第 32 字节之后就是乱的。
所以今天恢复不出比 `41(1)` 更好的东西 —— 这条是选择改指而不是重建的直接依据。

## 六、缺口

- **谁删的没查出来**。只有"同一租户管理员账号、真实控制台会话"这一条。
- `41(1)` 少 5 张宣传图（内容早已损坏），需要原始素材才能补。
- Dify 已发布 app `aiops-41-kefu-agent` 的**人工提问**没跑（只验了它依赖的检索通道）。
- `kb-canary` 那个租户的库仍是「只有视频、无文字」，问文字问题一律 0 条。
