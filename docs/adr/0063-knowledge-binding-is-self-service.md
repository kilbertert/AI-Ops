# 63. 知识库挂接是运营在产品界面里点选的，不是工程师填的表

日期：2026-10-20
状态：已采纳
影响版本：`src/aiops_diagnostics/gateway_store.py`（`dify_knowledge_bindings`）、
`gateway_api.py`（`/v1/knowledge/*` 与 `/knowledge` 页）、
`knowledge_retrieval.py`（`list_knowledge_bases`）

## 背景：配置知识库需要**三门知识**，而它们都不是业务知识

要把 Dify 的外部知识库接到 RAGFlow，运营此前必须：

1. 在 41 的 `production.env` 里写一行
   `AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS=kb-x:<tenant>:<dataset>`；
2. 知道自己的 RAGFlow 账号映射到哪个 **kb-service 租户 id**（这个 id 既不是 RAGFlow
   用户名也不是 UPMS 租户，是一条 SQLite 映射表里的行）；
3. 知道目标知识库的 **RAGFlow dataset id**（32 位十六进制）。

三样都只有工程师知道。结果是：需求本身（"能在前端点击完成知识库配置"）没被满足，
而每一次配置都要经过一个人肉步骤，且**填错时是静默失效** —— env 里多一个字符，
Dify 侧只会看到 404。

更糟的一个真实样本（2026-10-20 实测）：某个知识库建在 `admin@ragflow.io` 名下，
而**没有任何 biz 租户映射到它**。也就是说它在这个体系里根本没有主人 —— 无论把
`tenant` 填成什么，检索都不可能成功，而这一点从界面上完全看不出来。

## 决策一：绑定是一张表，不是一个 env 行

新增 `dify_knowledge_bindings(knowledge_id, tenant_id, knowledge_base_ids, …)`，
形状与 env 登记表**逐字段对应**，所以两者可以合并成同一张映射；env 行继续生效，
数据库行在冲突时覆盖它（写它的人是刚点了鼠标的运营，那是更新的决定）。

- **每次检索都读表**，不做进程内缓存：运营取消勾选后，下一句提问就该检索不到，
  而不是等下次部署。
- **没有 status 列**：解绑就是删行。"还能不能用"必须是一个只需要读一处就能回答的问题。
- `knowledge_base_ids` 用 `|` 分隔存储，与 env 语法一致 —— 这里没有查询需求，
  JSON 的表达力用不上，而读的人少学一种编码。

## 决策二：可选库只看**调用者自己的租户**

`GET /v1/knowledge/available` 用调用者的 tenant 去问 kb-service
（`GET /kb/knowledge-bases`），返回它名下的库。

这条刻意的窄：展示别人的库会让"我该勾哪个"含糊，更坏的情况是让跨租户挂接看起来像个
普通选项。代价明写：**建在 `admin@ragflow.io` 名下的库在这里看不到**，它必须先在该
服务的租户名下重建，或由工程师作为显式例外绑定。这不是遗漏，是"谁能授权"的答案。

## 决策三：页面与 API 同域，页面本身不带凭据

`GET /knowledge` 是一张静态页（包内 `static/knowledge.html`），它**自己**向
`/v1/knowledge/*` 出示令牌。放在网关里的原因只有一个：同域。

换一个静态站会先撞 CORS，再撞"把令牌发给另一个源" —— 后者本身就是个坏主意。
页面不落盘保存令牌以外的东西，且令牌只存在运营自己的浏览器里。

## 被否掉的替代形态

- **继续用 env 登记表，只写个脚本让运营跑。** 那仍然是"找人跑一条命令"，
  而需求的原话是"在前端点击"。脚本还要拿到 41 的 SSH。

- **让调用者从请求里带 `tenant_id` 决定检索范围。** 这是把授权交给请求体。
  现在 `tenant_id` 在 body 里**只为对照**：与调用者不一致就 403，而不是被采信。

- **把 `knowledge_id` 直接等同于 RAGFlow dataset id。** 见下方「事实」一节：
  两者本来就是两个东西 —— 一个是 Dify 侧的名字，一个是 RAGFlow 侧的 32 位 id。
  让它们相等会把 Dify 的命名自由度钉死在 RAGFlow 的 id 上。

- **做一个独立的管理前端应用。** 一张 200 行的静态页 + 三个 API 就够；
  多一个前端工程意味着多一条发布链路、多一套鉴权。

## 需要写清的事实：**Knowledge ID 不是 RAGFlow 的 dataset id**

这两个 id 在界面上看起来很像，但它们属于两侧：

| 名字 | 属于谁 | 谁生成 | 例子 |
|---|---|---|---|
| `knowledge_id` | **Dify** | 运营在这里自己起名 | `kb-canary-2` |
| dataset id | **RAGFlow** | RAGFlow 建库时生成 | `8701c742b28111f18637d95f7710e3a3` |

Dify 每次都把**它自己那个名字**发给我们（`POST {endpoint}/retrieval` 的
`knowledge_id`），我们查表把它翻译成**一个或多个** dataset id，再去 kb-service 检索。

所以：在 Dify 的「External Knowledge API」对话框里那个 *Knowledge ID* 输入框，
填的是**我们表里的键**（比如 `kb-canary-2`），**不是** `8701c742…`。
填 dataset id 会拿到 404 `DIFY_KNOWLEDGE_NOT_FOUND` —— 那个 id 从来不是这张表的键。
（2026-10-20 实测：拿 `fdcb2570cc6311f18637d95f7710e3a3` 直接当 `knowledge_id`
调用，得到 404；用登记的 `kb-canary-2` 则进入检索。）

## 后果

更容易：运营在本租户的库里勾选 → 起名 → 保存 → 把这个名字填进 Dify，完成。
不需要知道 tenant 映射，不需要改 env，不需要重启。

更难：**跨租户/无主库仍然要工程师介入**。这是刻意的边界：那个动作改变的是
"谁能读到哪些数据"，不该是一个复选框。

上限：只解决"用哪个知识库"，不解决"库里的内容对不对"——文档解析、embedding 模型、
分块质量仍在 RAGFlow 一侧。本次还实测到：RAGFlow 的 embedding 走的是**欠费**的
baoyun key，`/embeddings` 403 `insufficient_user_quota`，于是检索整体 502。
那是充值问题，不是接线问题；它会让上面所有步骤看起来"配了但没用"，所以必须记住：
**先确认 embedding key 有余额，再怀疑配置。**
