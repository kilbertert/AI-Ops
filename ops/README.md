# 生产访问边界

当前产品路径与交付状态见[当前说明](../docs/system-architecture.md)。本文件是部署/维护方法，配置默认关闭不代表现役全部关闭；
新旧执行器和场景分别验收，不按一个服务进程推断全系统已迁移。

TDengine Community Edition 3.4 允许非超级用户写入已有数据库，并且拒绝
`GRANT READ`。因此生产诊断不得通过 SSH 直接转发到 6041 端口。

`aiops-tdengine-readonly-proxy.service` 运行在数据库主机 loopback 接口上，只接受本项目发出的精确
`SHOW STABLES` 和有界 `SELECT` 语句。上游 TDengine 凭据只保留在该主机。Phase 3a 后工程师使用的 SSH key 只能转发到 TDengine 只读代理；MySQL / Redis 证据改由 `/diag/*` HTTP 诊断接口读取，不再保存直连凭据。

代理必须使用 root 所有的环境文件部署。客户端凭据和上游 TDengine 凭据都不得放入本仓库。回滚生产访问路径时，必须一并移除 service、service account、上游用户和 SSH `permitopen` 配置。

# Dify 可达的公司系统面（`ops/dify-exposure-registry.md`）

面向 Dify 暴露的**每一个端点**、它们的鉴权与只读语义，都登记在那份文件里；集合**之外**
（数据库、Redis、kb-service / RAGFlow 内部控制面）一律不可达。`tests/test_dify_exposure_registry.py`
在 CI 里断言"声明的集合 == 路由表里面向 Dify 的那些"，**新增端点却漏登记即失败**（#583 / PRD #577）。
运行时与网络面探测是 `tools/check_dify_exposure.py`（段 2/段 3，不进 CI）。

# 环境清单与 Agent 收敛（ops/environments/）

`ops/environments/<env>.toml` 是**一个文件、两个不同权威的段**：`[[agents]]`
是**发布权威**，`[[dify_apps]]` 是**运行时注册表**。两者的定位不同，清单把它们
分开写而不是混为一谈：

- **`[[agents]]`（发布权威）** 声明一个环境应有的已发布客服/运维 agent。
  `aiops admin reconcile` 通过 AgentManager（与 /v1/agents HTTP API 完全相同的
  角色校验、模型白名单、发布前 KB 活性校验代码路径）把 gateway 库幂等收敛到清单
  ——取代环境 UPMS 未建 ROLE_AGENT_ADMIN 角色族时的 on-box sqlite 手工插库。
- **`[[dify_apps]]`（运行时注册表，不是发布权威）** 声明 `(租户, 业务入口)` 由
  哪个 Dify app 服务。它**不发布任何东西、也不版本化任何东西**：从 Dify 拉来的
  agent 仍然由我们自己的发布动作发布。它记录的只是运行时读的那条映射。
  Dify 里**完全没有租户概念**，所以租户判定只能在我们的运行时完成，这张表就是
  那条边界的落点。

```text
[[agents]]                  # 每个期望的 agent（发布权威）
tenant_id / name / description
agent_type                  # customer | operations
prompt                      # 1-8000 字符；**绑定了 Dify 的 agent 不写这一行**（见下）
knowledge_base_ids = [...]  # 发布时对 kb-service 做活性校验
model                       # 必须在 Settings.providers 白名单内
output_contract             # 可省，按 agent_type 推导（customer→blocks-v1）
opening_questions / quick_commands = [...]   # 可省
state = "published"         # published | disabled，默认 published

[[dify_apps]]               # 运行时注册表（#584 / PRD #577）
tenant_id                   # 租户
business_entry              # consumer | operator（= faq.PLATFORMS）
app_id                      # Dify 侧 app id：拉取面（dify_dsl_pull）按它导出
agent_name                  # 该 app 落到我们库里的 agent **名字**
```

`agent_name` 是名字而不是 `agent_id`：id 每次重新发布都会变，而名字已经是
`agent_manifest` 的收敛键，也是同一个租户内唯一的。四字段**全部必填、没有一个
是推导出来的**——注册表的全部意义就是"运行时被告知哪个 agent 服务这个入口"，
而不是自己算出来。

### 一个 agent 的正文**只能有一个来源**（#739 / PRD #577）

两条规则互为反面，`load_manifest` 在**任何写入之前**就会判：

| 这个 agent | `[[agents]]` 里的 `prompt` | `[[dify_apps]]` 里的绑定 |
|---|---|---|
| 由 **Dify** 编辑 | **不写** | 写 |
| 由 **清单** 承载 | **必须写** | 不写 |

- 绑定的 `agent_name` 必须在本清单的 `[[agents]]` 里声明过（同租户内），
  否则失败：注册表行和它指向的 agent 是同一个事实，半个错的事实不能先落库。
- **两者都写**会被拒：同一份正文两个权威，而线上该用哪一份没有规则可答。
  本仓确实撞上过（Dify 上 129 字存根 vs 清单里 759 字真稿）。
- **都没有**（未绑定又不写 prompt）也会被拒：那这个 agent 就没有正文来源了。
- **库中还没有这个 agent、正文又不在清单里** ⇒ 收敛**拒绝创建**（报 `manual-action-required`），
  绝不把"正文在别处"这个状态发布成线上提示词。
- **从 Dify 切回清单**（写回 `prompt`、去掉绑定）时，库里的旧绑定**必须先清掉**
  （`reconcile --prune`），否则注册表那条旧行还在、线上仍是两个来源。
- 同一条规则也管 `app_id`：**一个 Dify app 只有一个 `pre_prompt`**，所以它不可能同时是
  两个 agent 的正文权威。同一个 `app_id` 出现在两行上，是把「两个权威」从 prompt 维度
  搬到了 app 维度。

### 拉回：`admin pull-dify`，知识库绑定**必须显式给**（#577 拉回那一跳）

```bash
aiops --config /etc/aiops-41/production.env admin pull-dify \
  --app-id <dify app id> --tenant <tenant> --name <agent 名> \
  --agent-type customer \
  --knowledge-base-id <我们的知识库 id>      # 可重复；不填 ⇒ 见下
  --db /var/lib/aiops-41/gateway/gateway.db --kb-url http://127.0.0.1:29380
```

**DSL 里的 dataset id 不是我们的知识库 id，两者不是同一个 id 空间。** Dify 的
`dataset_configs.datasets[].id` 是它的 dataset UUID；`AgentConfig.knowledge_base_ids`
是 kb-service 用来校验的那个 id。把前者按字面搬过去，发布门会以
`知识库 df0f1d4a-… 不存在或不在当前租户下` 失败 —— 那是**设计如此**，不是故障。

| `--knowledge-base-id` | DSL 里有 dataset | 结果 |
|---|---|---|
| 没给 | 有 | **拒**（点明两个 id 空间不同，不按字面搬运） |
| 没给 | 没有 | 库里**已存在**这个 agent ⇒ 保留它的现值；新建 ⇒ `()` |
| 给了（可多个） | 任意 | 按字面采纳 |

⚠️ **最后两行是两种不同的意图，不要混**：「这次拉取没提知识库」≠「这个 agent 没有
知识库」。拉一个 dataset-free 的 app 时后者会让线上客服**悄悄摘掉 RAG 路径**，
而命令照样退 0。真要去掉绑定就用**别的显式动作**，不要靠省略参数。

**fail closed。** 注册表为空时（清单里没有 `[[dify_apps]]`）保持注册表出现之前
的选法不变——所以这个开关可以**惰性地**上线，未采用它的环境行为逐字不变。一旦
表里有**任何一行**，未登记的 `(租户, 入口)` 就**什么都不选**：不回退到最新已发布的
agent，也不落到零阶回答。回答是 `retrieval_status="unavailable"`（不是
`not_found`）——什么都没检索，说"库里没有"就是一个没人做过的断言。

on-box 收敛（41 为例）：

```bash
aiops --config /etc/aiops-41/production.env admin reconcile \
  ops/environments/env-41.toml \
  --db /var/lib/aiops-41/gateway/gateway.db \
  --kb-url http://127.0.0.1:29380
```

`--db` 必须显式给出：`--config` 只喂 Settings（模型白名单等），数据库
路径走 `GatewayServerSettings.from_env()`，仅读进程环境变量。不 source
production.env 就执行时，会回退 XDG 默认
（`~/.local/share/aiops-diagnostics/gateway/gateway.db`）**静默新建空库**，
收敛结果全 `created` 而非预期 的 `unchanged`。

- 首跑应全部 `unchanged`；出现 `updated` 说明清单转写有误，停手修清单。
- `--prune` 仅作用于清单中出现过的租户（多余 published → disable，未发布
  draft → delete），绝不动清单外租户；`--dry-run` 零写入输出计划。
- `[[dify_apps]]` 在同一命令里收敛（输出 `dify_registry` 段），与 agent 收敛
  是同一个已声明状态：拆成第二条命令会让两者在两次调用之间走偏。未变更的绑定
  不写库；库里有、清单里没有的绑定**只在 `--prune` 下删除**（与 agent 删除同一
  个开关，理由相同：删除是需要被要求的方向，多出来的绑定是**可见的**——运行时
  仍在服务它，报告也点它的名）。
- 清单内模型不在白名单时在任何写入前失败（exit 2，库零变更）。
- 写的是运行中 gateway 的 sqlite（WAL + 短事务），首跑建议低峰窗口并先
  `cp gateway.db gateway.db.bak-<date>`。

### 运营改完 Dify 之后：`admin sync-from-dify`（无参数）

```bash
aiops --config /etc/aiops-41/production.env admin sync-from-dify \
  --db /var/lib/aiops-41/gateway/gateway.db --kb-url http://127.0.0.1:29380
```

和 `pull-dify` 的分工：**`pull-dify` 是装配**（7 个参数：某个 app → 某个租户的某个 agent，
它会改路由/类型/KB），**`sync-from-dify` 是例行**（零参数：把「现在生效的那一版」更新成
Dify 现在这一份，**只改正文与模型**）。运营改完正文之后走的是后者。

七个参数全部从库里推：`app_id`/`agent_name`/`tenant_id` 来自 `dify_app_registry`
（`[[dify_apps]]` 收敛的），`agent_type`/`output_contract`/`description`/`knowledge_base_ids`
来自库里那个 agent 现在是什么。所以它不会顺手改掉线上路由。

**幂等靠比较。** 拉回的正文与**已发布**版本逐字相同 ⇒ `unchanged`，一行不写。
Dify 侧真的变了才 `synced`。所以这条命令可以放心重复跑。

**为什么是显式动作、不做定时器**：Dify **没有草稿区**（41 实测：保存即导出，未点 Publish
的正文立刻可被导出），定时同步会把运营**打字打到一半**的正文推给真实用户。
触发的前提是有人知道"我改完了"。

**拒的三档**（报 `refused`，都不写库）：注册表点名的 agent 不存在 / 不是 `published`
状态 / 已发布版本的类型与当前 agent 不一致。这三档都要求人先解决，而不是让同步去覆盖。

### 让运营自己在 Dify 里点一次就发布：`POST /v1/dify/publish`

`sync-from-dify` 解决了「不用记 7 个参数」，但触发它的还是**运维**（要 SSH 进 41）。
这一步把触发交给**运营自己**：他在 Dify 控制台里改完正文，调一次这个路由即可。

```
POST https://api.mall.qushiyun.com/v1/dify/publish
Authorization: Bearer <AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY>
Content-Type: application/json

{"app_id": "8e36f0a6-324c-4003-b573-7444cc649411"}
```

**请求体只有一个 `app_id`**，而且必须是 `[[dify_apps]]` 里已经声明过的那个。
租户、agent 名、类型、输出契约、知识库绑定**全部从运行时注册表读** —— 所以拿到这把钥匙
的人不能加绑定、改路由、换类型、挂知识库、注入任意正文。最坏情况是**重新发布一个已声明
绑定的当前内容**，而内容没变时它什么都不写。

**它必须用一把独立的密钥**（`AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`），不能复用：
- 复用知识检索那把 ⇒ 「能读知识库」等于「能发布」，两件事被绑成一个权限；
- 复用控制台那把 ⇒ 那把是**服务端**凭据，交到触发方手里就不再是服务端的了。

三把钥匙同值、或「配了发布密钥却没配控制台凭据」，都是**启动错误**（不是运行时才发现）。

**为什么不做成定时器**：Dify 的 chat app **没有草稿区**（`export_dsl` 读
`app.app_model_config_id` 这个单指针，保存即覆盖）。定时拉取会把运营**打字打到一半**的
正文推给真实用户 —— 这一条是实测定下来的，不是推断。

**响应**：`{"action": "synced"|"unchanged", "version_no": N, ...}`。
`unchanged` 表示 Dify 与已发布版本逐字相同，什么都没写。失败分别是 401（无凭据）、
403（凭据不对）、404（未配置 / app 没绑定）、409（发布门拒了）、502（Dify 或控制台不可用）。

**运维侧的等价命令**仍是 `aiops admin sync-from-dify`（零参数，跑**全部**绑定）。
两者调用**同一个函数**，所以不会漂。

# WF01知识回调部署

与[WF01配置](../docs/agents/consumer-wf01/README.md)一起部署的片段为
`ops/nginx/dify-consumer-callback.conf`，目标位置：
`/www/server/panel/vhost/nginx/extension/api.mall.qushiyun.com/dify-consumer-callback.conf`。
它精确匹配`POST /v1/internal/consumer-knowledge`，保留Dify的一次性Authorization，清空业务身份头；
不能放进消费者H5的服务令牌include，也不能经通用App `/v1/`代理替换凭据。

从已合并提交取制品，先备份已有同名片段（不存在也记录），再用`dev-host cp 41 <制品> --dest <目标路径>
--artifact-sha256 <摘要>`部署；保持0644。经`dev-host exec 41 --allow-service-exec --`执行
`/www/server/nginx/sbin/nginx -t`，通过后同一二进制`-s reload`。
失败时恢复原片段；原来不存在则只删除本次新增文件，重新检查并reload。
回执分别记录制品摘要、配置校验、reload、回调结果，不能把上传成功当部署完成。
用已撤销验收凭据只读复核`GRANT_REVOKED`而非`GRANT_UNKNOWN`；新有效凭据不得代发或重放。

# 消费者 H5 的助手面转发（`ops/nginx/`）

`ulink.h5.mall.qushiyun.com` 是个只有静态回退的站点：`/v1/` 下的请求不到网关，
`GET` 拿到的是 SPA 回退的 `index.html`（gzip 780 B），`POST` 直接 405。
于是聊天页唯一的症状是 "Failed to ask"，而访问日志里那几条 `200` 看着像成功。

`ops/nginx/ulink-h5-assistant.conf` 是装到该 vhost `extension/<host>/` 的 server 级
include，**只转发 H5 聊天页实际调用的 12 条路径**。清单的唯一声明处是
`ops/nginx/h5-assistant-routes.txt`；`tests/test_h5_assistant_routes.py` 用**真的 nginx**
断言「声明的都转发」+「网关其余 76 条一条不转发」。

**按方法拆 location，不写一条路径正则**：`location ~` 只匹配 URI、不看方法，
一条正则会顺带放开 `POST /v1/shortcuts`（创建快捷键，管理面）。
`limit_except` 也不够 —— 它在静态回退之后才生效，实测 `POST` 照样进代理。

宿主安装属于**产物部署**（CD 只管 `src/` 到 `/opt/aiops-41`），
按 `deploy/manual-deploys.md` 留痕。回滚 = 删两个文件 + `nginx -t` + reload。

新版WF01同一片段增加精确范围`/v1/internal/consumer-data/(knowledge|status|billing)`的POST转发，
保留旧consumer-knowledge供旧发布版与显式恢复使用。新凭据在Gateway会拒绝旧回调，不能靠URL切换能力。
Nginx仍清空消费者/运营身份头，只透传本次grant。未匹配的后缀不能落入此内部转发规则。
