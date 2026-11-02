# 站点代理验证

1. 临时数据库、合成公司身份及动态端口 CPMS；运行 `tests/operations/test_site_tool_proxy.py`。
2. 编译实际节点源码，使用 `GatewayClient`、`CpmsGatewayAuthorization` 和 `CpmsReadTool`，
   对真实 Gateway 依次领取授权、取数、再次鉴权、上报证据。正例必须让 Gateway 接收证据。
3. 覆盖空参数和非空关键词、管家受限/全租户/空范围、消费者本人范围；上游跨租户、
   错误请求目标、撤销及多次领取后缓存证据仍可验证。合成数据不代表真实业务验收。
4. TLS 模板实际启动并驱动所有声明路径，其他路径与方法拒绝；保留现役 TLS 和模块配置。
5. 合并制品成套部署后，以正常原 H5 账号各发送受控站点及订单请求，按精确 QA/run 核
   版本、证据和独立 CPMS。前后核正式绑定一致，旧制品和配置保留供成套恢复。

本地进程和临时根由测试关闭；原始线上账号、响应和截图留私有目录，真实审计不删除。

执行命令（依赖沿用现有锁定 JDK 和包含 `lib/` 的 Java 依赖目录）：

```bash
M3NODE_DEPS=<依赖根> M3NODE_JDK_IMAGE=<JDK镜像digest> \
  uv run python tools/experiments/m2_close/m3node/verify_site_proxy.py --out .scratch/site-proxy
uv run pytest -n 0 tests/operations/test_site_tool_proxy.py tests/consumer/test_consumer_tool_grants.py
uv run pytest -n 0 tests/test_node_runtime_deploy.py
```
