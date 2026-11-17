# 275. 每环境节点固定回调，平台与模型服务共享

Status: accepted（已批准开通规划的执行切片，现场验收另记）

新环境的Java节点部署在共享服务区，独立Snail应用/节点凭据、状态根及唯一注册名称；
复用现役平台与模型网络，不复制Snail、平台数据库或模型出口到客户服务器。
节点仍只连接internal网络；另一个无业务凭据的Nginx出口连接独立callback网络和egress，
只代理工具声明中的路径到一个固定HTTPS环境。两段TLS分别校验内部证书和目标正式证书，
不实现CONNECT、不从请求选择目标、不回落其他环境。部署配置由运维登记，不接受模型/普通请求输入。

替代方案：节点直接连通用egress最少配置，但违反[ADR0041](0041-model-egress-is-a-fixed-target.md)；
复用现役node动态切环境会改权限边界；宿主额外监听则增加端口与防火墙配置。
因此采用独立Compose项目，仅两容器且无宿主端口，共享组件只作为已存在external网络依赖。

Compose自动给服务名注册DNS；新服务不用现役`node`这个名称，并额外配置全局唯一的节点别名，
防止平台按旧名称派发到新节点。每环境单独应用token，不能让同一执行组负载均衡跨环境。
只复用原工具模板与声明生成白名单；不修改现役render/render_ingress的参数或语义。

依据[Compose网络](https://docs.docker.com/reference/compose-file/networks/)的internal/external语义、
[Nginx上游TLS验证](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ssl_verify)。
代理镜像固定linux/amd64摘要，不使用浮动标签；节点JAR继续按现役部署锁核验。
资源上限不是容量验收，能力心跳不是合法run；完整认证、授权查询、模型与故障恢复另验。
