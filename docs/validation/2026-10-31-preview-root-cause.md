# 草稿预览的授权约束（2026-10-31，实测）

## 结论

**孤立签发一张 `data_grant` 不足以做草稿预览。** 网关会在
`_authorized_consumer_snapshot` 里校验它**绑定了一次真实运行**：

```
grant.workflow_id ↔ run.workflow_id 一致
且 _same_tenant_as_grant(run.tenant_id, grant.tenant_id)
且 latest.state ∈ {RUNNING, SUCCEEDED}
否则 → 403 DIFY_RUN_NOT_ACTIVE
```

没有 `chat_dify_runs` 行时拿不到快照，插件抛 `ValueError`，
外层报 `503 KNOWLEDGE_UNAVAILABLE`（掩码了真正原因）。

## 我这一轮做的（全部已回滚）

1. 用 `DataGrantStore.issue()` 签发短期授权（`environment=production-41`、
   `tenant=1942105476598861824`、`workflow_id=7847fe49…`），**每次都在 `finally` 里 `revoke`**。
2. 用它调 `POST /v1/internal/consumer-knowledge`：
   - 直连 `172.18.0.1:8788` → `503 KNOWLEDGE_UNAVAILABLE`
   - 走公网 `api.mall.qushiyun.com` → 同样 `503`
   - 两条链路**都到达了我们的网关**（不是网络问题）
3. 复核基础依赖，排除外因：
   - 目录接口带正确目录凭据 → **200 / 458 字节**（直连与公网都通过）
   - `KbServiceClient('http://127.0.0.1:29380', tenant_id='1942105476598861824').list_knowledge_bases()`
     → **5 个库正常返回**
   - kb 隧道 `aiops-36-kb-tunnel.service` = active

即：**目录、检索、隧道都是好的**，卡在"授权必须绑运行"这一条。

## 三条被我自己推翻的错误判断（保留过程）

| 我先前的说法 | 实测真相 |
|---|---|
| 「advanced-chat 不支持草稿预览」 | `/advanced-chat/workflows/draft/run` 返回 **200**，只是我把路径试漏了 |
| 「公网 `/v1/internal/` 被拦截 403」 | 用 GET 打了 POST-only 端点才得 403；正确方法下公网**能到**网关 |
| 「插件 `gateway_url` 指向不可达地址」 | 插件配的是根地址，实测**可达**；问题不在这 |

三次都是**我读错了自己的测试结果**，而不是环境坏了。
规律：看到非 2xx 就先假定"环境/能力限制"，而没有先核对**我的调用方式是否正确**
（用错方法、试漏路由、拿错作用域）。

## 现在的位置

要做真实预览，必须产生一条**受管运行**（`chat_dify_runs`），
仓库里对应的路径是 `consumer_trial` / `dify_workflow_trial`。它们会：
- 写 `chat_dify_runs`、`chat_data_grants` 行；
- `WorkflowTrial` 还会在生产 workspace 短暂导入并删除一个临时应用。

这一步的写面比"签发一张授权"大，需要单独确认。
**本轮到此为止，未改任何生产配置、未建临时应用、授权已全部撤销。**
