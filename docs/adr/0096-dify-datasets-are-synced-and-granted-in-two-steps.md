# 96. Dify 的 dataset 由我们同步，权限靠第二次请求才成立

日期：2026-10-23
状态：Accepted
影响版本：`src/aiops_diagnostics/dify_knowledge_datasets.py`（新）、
`src/aiops_diagnostics/dify_console_admin.py`（+3 方法）、`admin_cli.py`（+`sync-knowledge-datasets`）、
`tests/test_dify_knowledge_datasets.py`（新）

## 背景

#1000 让 `knowledge_id` 能指向单个库（`tenant-<租户>:<RAGFlow dataset id>`）。那是检索侧的
一半。Dify 的编排页上，「一个知识库」就是**一条 external dataset** —— 没有那条 dataset，
运营在智能体配置里选不到，库级 id 也就没人用得上。

真机上「未找到知识库」的成因不是权限配置错了，而是**从来没有人建过那条 dataset**：Ulink
workspace 里活着的 3 条 external dataset 都是 ``only_me``，而登录的人是 ``editor``。

## 决策一：一条命令，`(workspace, external_knowledge_id)` 是幂等键

`aiops admin sync-knowledge-datasets --tenant … --workspace … --api-id …`

- **键是 knowledge_id，不是名字。** ``datasets`` 表**没有**名字唯一约束，只有服务层在建/改
  时查一次同租户同名。用名字做键，第二次同步会撞上"已经有一个叫这个的"；用 id 做键，
  名字只是**可被纠正的显示值**。
- **`knowledge_id` 与检索侧共用 `auto_knowledge_id`。** 两处各算一遍，就会出现"同步成功、
  检索 404"—— 而这个仓库里"同一规则两份实现"已经漂过三次。
- 不建外部知识库 API 模板：模板是每个 workspace 一份、由开户流程建的；命令只接受
  `--api-id`，不给就拒绝。猜一个模板等于把凭据绑到别处。

## 决策二：`permission` 只能靠**第二次请求**设上 —— 这是 Dify 的形状，不是我的选择

对着 Dify 1.17.1 的源码核过：

```
ExternalDatasetCreatePayload: external_knowledge_api_id, external_knowledge_id,
                              name, description, external_retrieval_model
                              ← 没有 permission
create_external_dataset():    Dataset(...) ← 不传 permission
Dataset.permission:           server_default 'only_me'
```

所以**建完必须再 PATCH 一次**。而 ``_update_external_dataset`` 对
``external_knowledge_id`` / ``external_knowledge_api_id`` 缺任一都 ``raise ValueError``
（不是"保持不变"）—— 也就是"只改权限"的请求会被 500。两个外部字段因此每次都带全。

**中间态是必然的**：创建与授权之间，那条 dataset 只对创建者可见。它是秒级、且只对创建者
产生影响（别人本来也看不见），所以不值得为它做别的事。但它**必须**写在这里 —— 下一个人
读"建 dataset"这一步，会以为一次请求就完事。

## 决策三：名字上限取**两个接口里更严的那个**（40 字节）

创建允许 100 字节，更新只允许 40（``DatasetUpdatePayload.name``）。
用 100 去建、再拿同一个名字去授权：创建成功、PATCH 422 —— 库里留下一条**谁都看不见**的
dataset，而错误只说"名字太长"。所以在**创建之前**就按 40 拒绝，并给出可行动的下一步
（改 RAGFlow 里的名字，或 `--name` 覆盖）。

## 决策五：``external_retrieval_model`` 每次 PATCH 都写 —— 不写就是"每条检索都崩"

这一条是 **#1002 真机抓出来的**，而且它把我上面那四个决定全部证成了「对，但不够」。

真机现象：dataset 建好了、`all_team_members`、编排页里选得到、**命中测试也能出结果** ——
可是智能体回答「抱歉，我这边暂时无法查询到…」。

Dify 的 api 日志给出真因：

```
File "/app/api/services/external_knowledge_service.py", line 520
    score_threshold_enabled = external_retrieval_parameters.get("score_threshold_enabled") or False
AttributeError: 'NoneType' object has no attribute 'get'
```

``fetch_external_knowledge_retrieval`` 收到的 ``external_retrieval_parameters`` 就是
``dataset.retrieval_model``，而 ``create_external_dataset`` **不写**这一列 ⇒ SQL NULL ⇒
对 None 调 ``.get``。Dify 把这个异常吞成"没有检索结果"，所以症状是**一句礼貌的"查不到"**，
而不是一个错误。

命中测试为什么能过：它走的是 ``ExternalKnowledgeHitTestingApi``，那条路径读的是
``dataset.external_retrieval_model`` 这个**属性**（``self.retrieval_model or {top_k:2,…}``），
有默认值兜底。同一个库，两条路径，一条兜底一条不兜底。

所以：**每次 PATCH 都带 ``external_retrieval_model``**，取值与 Dify UI 的
``DATASET_DEFAULT`` 一致（top_k 4、阈值 0.8、默认关闭阈值）。这样"我们同步出来的"
与"人点过 Settings 保存的"是同一个状态 —— UI 的表单每次也都会带这一块。

**并且 ``plan()`` 会把它当成待修项**（``fix_retrieval_model``）。只判「在不在 + 权限
对不对」的 ``plan()`` 会永远认为#1001 建出来的那批是健康的 —— 而它们每一条都检索不了。

**这个待修判据不能写成 ``not item.get("external_retrieval_model")``。** 第一版就是这么写的，
在 41 上**一次都没触发**：列表接口回的 ``external_retrieval_model`` 是
``dataset.external_retrieval_model`` 这个**属性**的投影，而那个属性是
``self.retrieval_model or {"top_k": 2, "score_threshold": 0.0}`` —— 列是 SQL NULL 时它
照样回一个"看起来正常"的对象。41 上真实看到的是：

```
{"top_k": 2, "score_threshold": 0.0, "score_threshold_enabled": null}
```

所以判据落在 ``score_threshold_enabled is None``：兜底字典里**没有**这个键，只有真被写过
才会有值。这是"用响应里的兜底值伪造了一个健康状态"—— 一个判据看起来在工作、实际永不触发，
比没写更糟，因为它会让人以为重跑同步就能修。

## 决策四：RAGFlow 里已删的库，**报告，不删**

删 dataset 在 Dify 里不可逆，而且删一条正被智能体引用的库会静默破坏那些 app。命令把它们
列进 `orphans`，由人决定。

## 被否掉的替代形态

- **开户时顺手建（`onboard-tenant` 里做）。** 开户是新租户的一次性动作，而库是**之后不断
  新增**的。把同步塞进开户，等于"新建的库永远同步不到"—— 那正是今天的问题。

- **Dify 的 API 拖（把 endpoint 配成我们的 `/v1/dify/knowledge`）。** Dify 的「连接外部
  知识库」是**手填一个 id**，不是从清单里发现 —— 清单路由（ADR-0066）让那一步变成下拉，
  但下拉里选中的仍然是**一条** id，不会**批量**建出 N 条 dataset。两者解决的不是同一件事。

- **给 `only_me` 的那几条直接把 maintainer 改成登录账号。** 只对已有的 3 条有效，下一条
  新库还是不会出现；而且 maintainer 是该 dataset 的归属事实，改它是在改别人的记录。

- **用 Dify 的 Postgres 直接写。** 绕过服务层等于绕过它的名字查重、RBAC 同步与事务边界；
  而且这是我们**不该**拥有的写路径（ADR 里反复的同一条：改别人产品请走它的门）。

## 后果

更容易：RAGFlow 里建一个库，跑一次同步，编排页里就能选到 —— 且是**整队可见**。

更难：**多了一步运维动作**。库是新的，命令要人跑。把它自动化（定时同步 / RAGFlow 建库
时回调）是下一个决定，不在本决策里。

上限：命令只建/改 dataset，不碰智能体配置。`orphans` 需要人看。名字超过 40 字节的库需要
`--name` 或改名。``external_retrieval_model`` 用的是 Dify UI 的默认值，不跟随每个库的
真实需要调 —— 要调就之后在 UI 里改，同步不会再覆盖（``plan`` 只在它是 null 时才动它）。
