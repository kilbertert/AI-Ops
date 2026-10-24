## 2026-10-24 · 桥也要读 changed：真机验证抓到的第三处「只看新增」

**要解决的问题**：ADR-0125 修完网关和前端之后，真机验收仍然看到页面不刷新。

用 Playwright 打开生产 Dify 的知识库页，判据是「响应体里有没有 `changed`」：

```
打开 /datasets 触发 knowledge-sync: 1 次
响应含 changed 字段: False  ['{"created": 0}']     ← 卡在这里
切回来触发 knowledge-sync: 2 次
页面含「41(1)」: True
判据通过: False
```

网关版本是对的（`0.2.0+9360fa4f2a75`），前端编译产物里
`changed?n.changed:"number"==typeof n?.created?n.created:0` 也在。**问题在中间的桥**：
`_knowledge_sync_payload()` 只取 `payload.get("created")`，`changed` 在那一行被丢掉。

**为什么漏了三轮**：桥的这条路径**零测试**。网关有路由测试、前端有编译产物核对，
唯独「网关 → 桥 → 浏览器」中间那一跳只靠真机看。

同一个「只看新增」的错误在本轮出现了三次——网关、前端、桥。前两处各有判据所以被
review 和编译核对抓住；桥没有判据，就活到了真机上。

**做法**：`changed` 优先、回落 `created`（兼容旧网关）；桥的响应同时带两个字段
`{"changed": N, "created": N}`，前端 `changed ?? created` 新旧都不读错。
**字段名一并改掉**，不只是取值——留着 `created` 这个名字会引诱下一个人接着只数新增。

**判据**：桥层新增两条单测（`changed` 优先、旧网关回落）；`test_dify_console_publish.py`
239 passed；真机复验 `{"changed": 0, "created": 0}`、挂载 1 次、切回 2 次、真实库名都在。

**部署**：桥在 36 上是手工拷贝的（`/opt/aiops-console-bridge/`，不在 CD 范围，见
`docs/agents/bridge-deploy-scope.md`），本次已拷贝并 restart。PR #1056 是把那份改动
固化进仓库，避免下次部署被静默抹掉。

参见 ADR-0125、`docs/validation/2026-10-24-bridge-knowledge-sync-changed.md`。
