# 环境工具HTTPS扩展入口

本片承接[开通预检](../../../docs/agents/environment-onboarding-preflight.md)的共享执行侧→目标环境方向。
使用公司既有HTTPS虚拟主机扩展目录；不改公司代码、不新建端口、不建立业务SSH隧道。
该片段只开放现役工具回调，运行凭据及租户权限仍由Gateway验证；不是用户聊天或管理页入口。

## 生成固定制品

先只读核对已有域名、TLS证书、server的include位置、`/v1/internal/`旧路径及实际访问，
确认没有现用路径被遮蔽。目标必须有Real-IP模块，且来源是实际连接方；前置代理场景不能
把伪造的X-Forwarded-For当来源，也不能仅把公共代理IP加入白名单。
核HTTP/server/default-server的错误日志级别不得为`info`或`debug`，且没有debug_connection；
请求头解析发生在location选择前，重复Authorization等畸形头可被这些级别记录，片段无法屏蔽。
不符合时拒绝安装，另行评审日志配置，不能未经授权关闭公司日志。目标只读检查与原始摘要留私有记录。

```bash
bash ops/environments/gateway/build-ingress.sh <已合并SHA> /private/ingress.tar.gz \
  http://127.0.0.1:<接入模块端口> <共享执行侧IPv4> <共享Dify侧IPv4>
```

在开发机运行，包内只有`REVISION`、`SHA256SUMS`和生成片段；来源地址是私有部署参数，
制品及其摘要留私有运维账本。源代码/工具声明来自同一合并修订，不能用未合并工作树生成生产配置。
生成器拒绝远端上游、网段、重复来源和指令注入；可用`render_node_callback.py --ingress`
在隔离本机测试，其他模式的既有节点渲染接口保持不变。

## 安装与恢复边界

安装前单独取得目标虚拟主机扩展路由与Nginx reload授权，记录来源、固定回环端口、
新文件的唯一名称、现役配置摘要、备份位置及实际流量观察。以下是固定步骤，不授权任意改动：

1. 通过`dev-host cp --artifact-sha256`上传已合并制品；核外层和包内摘要，拒绝未知或不匹配文件。
2. 备份本次目标片段（初次不存在也记账），先`nginx -t`确认原配置正常。
3. 仅安装本次扩展文件，保持root所有且不可被组/其他用户写入，再`nginx -t`。
   语法失败恢复原文件，不reload；成功才reload并读取实际响应与进程状态。
4. 核原公司API基线，分别从实际共享调用侧验证TLS、匿名拒绝、授权工具成功；
   非授权来源、明文HTTP、未知工具、错误方法应拒绝，伪造转发头不得放行。
5. 任一步失败恢复本次片段，语法校验后reload并复验原入口；不停止公司业务服务、不删除共享配置。

这不是通用安装器已交付；未通过目标验证不激活目录。配置保留固定路径白名单，
请求头只传运行Bearer、Content-Type及现有执行/工具合同头；片段不记录带订单参数的URL或上游错误正文。
此边界依赖上述宿主日志前置条件，不承诺任意宿主解析阶段日志都被片段覆盖。
例如TRACE在选择location前被Nginx拒绝，仍可能进入宿主原access log；任何调用均不得把
Bearer或其他秘密放在URL。需要连畸形请求URL也不记录的环境，应另行审批虚拟主机日志策略，
不能宣称此扩展片段完成了宿主级日志脱敏。
新增工具后从同一工具声明重新构建片段，禁止手工加宽到任意`/v1/*`。

## 验证

```bash
uv run pytest tests/integrations/test_environment_tool_ingress.py tests/test_node_runtime_deploy.py
```

真实Nginx与测试证书覆盖原公司路径、声明工具、身份头隔离、匿名拒绝、错误方法、未知路径、
伪造Real-IP及HTTP拒绝。最初仅在父location写来源if，被子路径的rewrite指令遮蔽；
检查现位于每个允许路径，保留原反例。测试上游是替身，不能证明真实公司数据或业务结论。
