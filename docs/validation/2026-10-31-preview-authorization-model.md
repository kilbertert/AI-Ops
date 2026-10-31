# 草稿预览的授权模型（2026-10-31，只读探测）

## 已确认

`POST /console/api/apps/<id>/advanced-chat/workflows/draft/run` **可用**（200，返回 SSE），
它执行草稿图、不改草稿也不改正式指针 —— 入口本身没问题。

**但它需要 `data_grant` 才能跑通。** 目标是 `knowledge` 类问题时，应用会调
`consumer_knowledge` 插件，插件回网关 `/v1/internal/consumer-knowledge`，该端点：

```
无凭据 → HTTP 401 {"code":"GRANT_REQUIRED","message":"knowledge grant required"}
```

`data_grant` 由 `chat_data_grant.DataGrantStore.issue()` 签发，**绑定**
`scope_fingerprint / order_no / environment / tenant_id / qa_id / workflow_id`，
明文只存在一刻，库里留哈希行。

全仓只有两处调用它：

| 调用点 | 用途 |
|---|---|
| `consumer_trial.py:137` | 受管试运行 |
| `dify_workflow_trial.py:58` | 临时应用 trial |

**没有"纯预览、不签发"的入口。**

## 这意味着什么

要在生产上真实预览"改后的提示词"，必须二选一：

- **A** 走 trial：会写 `chat_data_grants` 一行 +（若用 `WorkflowTrial`）在生产 workspace
  短暂导入并删除一个临时应用。
- **B** 用真实用户会话走正式链路 —— 但那跑的是**已发布**版本，不是草稿。

没有第三条"零写入地预览草稿"的路。这是授权模型的设计，不是漏洞：
知识检索必须带一张可追溯的授权，否则任何人都能借预览查任意租户数据。

## 我为什么停在这里

A 是写操作（哪怕可清理），且会往生产库写授权行。这与本轮"改提示词 + 发布"的授权范围
不是一回事，我不自行扩大。

同时补一条与任务书相反的证据：任务书说"不把离线夹具视为生产授权"——这点我完全同意，
而且已经实测确认**夹具确实穿不过去**（上面的 401）。所以"用夹具预览"这条路在技术上就不存在。

## 需要 owner 决定

是否允许为草稿预览签发**一张**短期、单用途的授权（并接受它在 `chat_data_grants` 留一行），
用完即撤销/过期？或者跳过预览、接受"直接发布后再验"的风险？
