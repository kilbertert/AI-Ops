# 正式节点运行部署件验证

代码：`feat/production-node-runtime`（基线 `44267218`）；环境：开发机一次性数据根。

- `tests/test_node_runtime_deploy.py tests/test_consumer_status_tools.py`：38 passed。
- nginx 真实进程：三种 POST 正确转发认证头、清单摘要与正文；GET、其他 API、非法 run 路径被拒。
- Playwright 使用实际 `_drive` 的离线回放：直读、渐进显示、失败、节点 queued 接单共四场景通过。
- `--execution node` 仅改变 POST 接单状态，保留同 qa_id 的 completed GET、no-store 与助手区域判据；
  failed 接单或 failed GET 均不能通过。默认 direct 不变。

这些测试不调用真实业务或供应商，不证明节点已在生产注册。未完成业务验收。
发布前静态检查和 CI 以本次交付回执为准；生产结果另记，不回填为本地成功。

改变仅为配置、部署流程和浏览器判据；不改业务授权/持久化算法，复杂度专项不适用。
代理白名单以真实 HTTP 反例验证，个人范围业务守卫沿用已有回归。
完整正式验收映射：`deploy/node-runtime/acceptance.feature` → `qa-plan.md` NET/UI/START/LIVE/RETIRE。
