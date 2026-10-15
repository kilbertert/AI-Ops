# 试运行的草稿由网关读：一条跨文件契约

日期：2026-10-15
状态：已采纳
影响版本：PRD #772 F 的部署层（子票 #795）；ADR-0022；`POST /v1/dify/trial`、
`ops/dify-console-bridge/`、`gateway_api.py`、`dify_console_publish.py`

## 背景

ADR-0022 定下了试运行的**执行**形状（同一条 harness 的另一个入口、护栏全继承、
预算同一份数字）。它同时把入口的 HTTP 面写成「`POST /v1/dify/trial` 提交 +
`GET /v1/dify/trial/{id}` 读结果」，并把"编辑页那颗按钮"留给部署侧，附一句关键约束：

> 草稿可读性的结论是：Dify 1.17.1 的 chat 应用没有草稿层，只能读到"最近一次保存"
> 的配置（#773 结论），所以按钮必须写明「以最近一次保存的配置为准」。

那份记录**没有回答**的是：**谁去把那份配置读出来、读成什么形状**。当时的请求体是
`{order_no, prompt, allowed_tools}` —— 也就是"调用方把草稿递进来"。这在测试与 CLI 上
成立，但控制台那颗按钮做不到：它只站在浏览器里，看不到方法论正文，也看不到工具勾选的
**服务端**标识。

于是部署层要定一个新问题，而它牵动三处：网关那条路由的请求体、那个薄桥的职责、以及
注入脚本挂几颗按钮。

## 决定

**草稿由网关自己读；桥只递 `app_id` 与 `order_no`；按钮由同一个注入脚本挂出来。**

请求体因此变成"两条草稿来源二选一"：

| 来源 | 字段 | 谁在用 |
|---|---|---|
| 显式草稿 | `prompt`（+ `allowed_tools`） | 测试、CLI —— 原 #781 的形状，逐字保留 |
| 服务端拉取 | `app_id` | **控制台按钮**（#795） |

两者**恰好给一个**：都填是歧义（谁赢？），都不填是"跑一次没有方法论的诊断"。
两者都拒，而不是挑一个 —— 挑一个会让调用方以为自己那半边生效了。

## 为什么是网关读（本记录真正要留的东西）

### 1. 草稿的"译文"规则住在网关，而且只能住一处

Dify 导出的 DSL 里，工具的条目**只有提供方的 UUID，没有提供方名字**
（`dify_dsl_pull.py` 模块 docstring 已把这一点写死：`AgentToolEntity` 是
`provider_type, provider_id, tool_name, …`）。所以"运营勾了哪七个工具"这件事，
**必须**当场从控制台解析 `aiops` 提供方的 UUID，再按 `partition_aiops_tools` 映射成
我们的 `ToolName`。

那套映射住在网关，而且发布路径正在用它（`resolve_pulled_config`）。它的 docstring
自己写着为什么只能有一份：

> This is the single place the pulled config is decided. Dry-run and the real
> pull both call it, so what a dry-run reports is *the same object* the real
> pull would publish rather than a second implementation of the same rules.
> (A hand-rolled copy in the CLI's dry-run branch is exactly how the retained
> knowledge binding came to be reported as empty while the real pull kept it.)

**这句话就是本决定的理由。** 让桥去解析那份 DSL，就是再造 `resolve_pulled_config`
记下的那个缺陷：干跑报的草稿与实际会发布的东西不一致，而两边都"看起来是对的"。

### 2. 桥**读不了** —— 这是硬约束，不是偏好

36 上的桥用系统 `python3` 跑（`/usr/bin/python3`，Ubuntu 24.04）。实测：**有 `yaml`，
没有 `pydantic`**。而 `dify_dsl_pull` 链条 import `pydantic`（经 `agent_contracts`）。
所以桥**在物理上**无法复用那套解析；它只能自己写一个"没有 pydantic 的简易版" ——
回到第 1 条。

### 3. 顺手得到的一致性

网关读草稿时用的 console 凭据（`AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL` + key +
workspace）与发布路径**是同一份**。于是"试运行跑的那份草稿"与"点发布时冻结的那份"，
用的是同一个 console、同一张绑定表、同一套映射 —— 不是"差不多一样"，是同一段代码。

## 被否掉的替代形态

- **浏览器读草稿再递进来。** 浏览器要能拿到方法论正文与工具的服务端标识，就得把
  两份 console 凭据交给它 —— 凭据进浏览器等于公开（ADR-0021 已否掉过同形的一条）。
  而且 `prompt` 上限 20000 字符，走注入脚本再回来是一条又长又脆的路。**否掉。**
- **桥读草稿（自己写一份解析）。** 见上面第 1、2 条。**否掉。**
- **新建一个注入脚本文件（`trial-button.js`）。** 见下节。**否掉。**
- **让请求体同时接受"prompt 或 app_id"且 app_id 优先。** 静默择一 = 调用方以为自己
  那半边生效了，而实际用了另一半。ADR-0022 对范围字段已经立过同一条规矩（多一个字段
  是 400，不是忽略）。**否掉。**

## 一处必须解释的注入约束（否则第二颗按钮会**静默不出现**）

`dify-console-10008.conf` 用 nginx 的 `sub_filter` 往 HTML 里插脚本，且开着
`sub_filter_once on` —— 它让 `</body>` **只被替换第一次**。

于是"再加一条 `sub_filter '</body>' ...` 让第二颗按钮从新文件挂出来"这条看起来最干净
的路**根本不生效**：第一条已经把 `</body>` 换掉了，第二条永远匹配不到。
**而且 `nginx -t` 不会报错、页面也不会报错** —— 第二颗按钮就是不出现。

所以决定：**第二颗按钮由同一个脚本挂出来**（`dify-publish-button.js` 现在挂两颗），
`sub_filter` 一行都不改。代价是那个文件名名不副实了（README 的部署表里记着它；
重命名会连带改 nginx、README 与 qa-plan 三处），接受 —— 留了注释说明它现在挂两颗。

（若哪天真要把注入挪到 TLS 那一层，必须**同时删掉 10008 内层那份**：`sub_filter`
是逐层独立的，两层都配就会出两份脚本，而同样不会报错。验收判据是页面里
`/__aiops/publish-button.js` 恰好出现一次。）

## 代价（写下来，不藏）

1. **网关多了一条"替调用方取配置"的路径。** 它的爆炸半径由绑定表限住（只有
   `dify_app_registry` 里登记过的 app 才读得到，未登记 ⇒ 404），但这确实是网关第一次
   因为"按钮需要"而主动去读控制台。
2. **试运行读到的不是"运营此刻编辑框里的东西"**，而是最近一次保存的配置（#773 的
   能力边界）。按钮文案必须写明，否则运营会以为它跑的是眼前这份。
3. **`app_id` 由 Referer 决定**（与发布同一处）。Referer 是请求上下文、**不是授权**
   —— ADR-0021 已经论证过；这里的授权仍是那枚 Dify 会话。

## 后果

- `POST /v1/dify/trial` 的契约变了：`prompt` 可选，新增 `app_id`，两者恰一个。
  显式草稿那条路逐字不变，所以 #781 的既有测试与 CLI 用法不受影响。
- 桥新增 `/__aiops/trial`，与发布**共用同一套浏览器三门**（Origin / Sec-Fetch-Site /
  Referer+CSRF）与同一份 CSRF 口径 —— 运营点的是同一张页面里的按钮，不该有两套门。
- 桥多了一把配置 `AIOPS_GATEWAY_DIFY_TRIAL_API_KEY`（未配 ⇒ 该路径 404，与网关同机制）。
  它与发布密钥在网关启动期被强制**互不相同**：能发布不等于能在真数据上跑一次诊断。
- 与 ADR-0022 的关系：**没有更正它的任何一条**。执行形状、护栏、预算、范围来源全部
  不变；本记录只补上它留给部署侧的那个问题（谁读草稿、读成什么形状）。
