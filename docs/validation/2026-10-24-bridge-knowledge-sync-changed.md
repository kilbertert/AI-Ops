# 桥读 changed：真机验证抓到的第三处「只看新增」

**范围**：`dify_console_publish.py` 的 `_knowledge_sync_payload` 与同步路由。
**已部署并真机验证**（生产 36）。

## 一、怎么发现的

ADR-0125 的修复上线后（网关 `0.2.0+9360fa4f2a75`、web 镜像 `160059d83651`），
用 Playwright 打开生产 Dify 的知识库页，判据是「响应体里有没有 `changed`」。

第一次结果：

```
打开 /datasets 触发 knowledge-sync: 1 次
响应含 changed 字段: False  ['{"created": 0}']     ← 卡在这里
切回来触发 knowledge-sync: 2 次
页面含「41(1)」: True
判据通过: False
```

网关版本是对的、前端产物里 `changed?n.changed:"number"==typeof n?.created?n.created:0`
也在。**问题在中间的桥**：`_knowledge_sync_payload()` 只取 `payload.get("created")`，
`changed` 在那一行被丢掉，页面收到 `{"created": 0}`，于是照样不刷新。

## 二、为什么漏了三轮

桥的这条路径**没有任何测试**。网关有路由测试、前端有编译产物核对，唯独桥——
「网关 → 桥 → 浏览器」中间那一跳——只靠真机看。

同一轮里这个「只看新增」的错误出现在三个地方：网关（`gateway_api.py`）、前端
（`knowledge-sync.ts`）、桥（`dify_console_publish.py`）。前两处各有判据，所以被
review 和编译核对抓住了；桥没有判据，就活到了真机上。

## 三、改法

```python
# _knowledge_sync_payload：changed 优先，回落 created（兼容旧网关）
for key in ("changed", "created"):
    value = payload.get(key)
    if isinstance(value, int) and value >= 0:
        return value
return 0
```

桥的响应同时带两个字段：`{"changed": N, "created": N}`。前端是
`changed ?? created`，新旧前端都不会读错。

**字段名也一并改掉**，不只是取值：留着 `created` 这个名字会引诱下一个人接着只数新增。

## 四、判据

| # | 判据 | 结果 |
|---|---|---|
| 1 | `changed` 优先于 `created`（桥层单测） | ✅ |
| 2 | 旧网关无 `changed` 时回落 `created` | ✅ |
| 3 | `tests/test_dify_console_publish.py` 全绿 | ✅ 239 passed |
| 4 | 真机：响应含 `changed` | ✅ `{"changed": 0, "created": 0}` |
| 5 | 真机：挂载触发 1 次 | ✅ |
| 6 | 真机：切回来触发 2 次 | ✅ |
| 7 | 真机：真实库名在页面上 | ✅ `41(1)`、`aiops-41-kb-prod-kefu` |
| 8 | 摘要锁 `--check` | ✅ |

## 五、部署

桥在 36 上是**手工拷贝**的（`/opt/aiops-console-bridge/`），不在 CD 范围内
（见 `docs/agents/bridge-deploy-scope.md`）。本次已拷贝并 restart，`is-active` 为
`active`。回滚 = 用 `rollback-pre-1054` 同期的备份文件覆盖后 restart。
