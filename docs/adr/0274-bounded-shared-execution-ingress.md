# 274. 共享执行平台只公开受限派发接口

承接已批准的手工开通规划：目标Python模块需经正式HTTPS调用共享Snail，平台与Java节点仍在共享服务区。
现有工具回调只覆盖相反方向。复用公司已有TLS虚拟主机的扩展点，唯一代理路径为
`POST /snail-ai/openapi/v1/agent/chat/sync`；平台管理、用户注册与节点gRPC继续保持内部访问。

来源使用原始连接IP，平台应用凭据原样交由Snail校验；只传两项应用头与固定JSON内容类型。
固定私有IPv4上游使用HTTPS并校验CA和证书DNS名称，不由请求选择主机，不重试到其他上游。
不公开整个Snail路径，也不把节点接入无约束公网网络或恢复SSH业务隧道。
独立节点的受控出站与环境绑定是后续依赖，不由新增入口自动建立。

依据[Nginx TLS上游校验](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ssl_verify)、
[原连接地址](https://nginx.org/en/docs/http/ngx_http_realip_module.html#var_realip_remote_addr)及
[请求头白名单](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass_request_headers)。
同步调用沿用Snail固定300秒执行窗口，310秒为代理相邻读取超时，调用者仍以run期限控制总等待。
部署必须核准来源、既有路径使用与日志继承；本片不修改现役平台/节点、数据库或租户启停。
