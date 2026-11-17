# 271. 环境工具回调复用既有HTTPS虚拟主机

状态：已采纳实现，实际安装需对应宿主的入口授权；承接手工开通规划P0/P3。

## 决定

共享执行服务通过目标环境既有HTTPS域名访问本机Gateway。生成server扩展片段，
仅承接`/v1/internal/`的现役节点/客服工具路径，不代理公司API或公开Gateway管理接口。
节点路径从原模板共享，工具名与CPMS路径仍从唯一工具声明推导；客服回调复用现役片段。
环境与租户权限、运行凭据继续由Gateway验证，来源白名单不构成用户授权。

每个允许location显式检查HTTPS和`$realip_remote_addr`，防止父级rewrite不继承和
共享宿主Real-IP配置把外来转发头当真实连接来源。首版只接受1至8个精确IPv4来源与
固定IPv4回环上游；不跟随失败切换到其他环境，不保留请求日志或透传任意业务身份头。

## 替代方案

- 单独新端口/虚拟主机能简化与公司路由隔离，但增加证书、暴露和运维成本；现有HTTPS扩展点已可复用。
- 直接复制节点白名单改地址容易落地，但新增工具曾因第二份名单遗漏而404，故共享原模板。
- SSH业务隧道可快速连通，但用户已否决，且与正式入口和通用开通方向不符。

## 后果与规格映射

`^~ /v1/internal/`外层限定命名空间，内部精确/正则location允许已有合同，最后拒绝未声明路径；
外层不会改变其他公司路径。来源检查与TLS检查必须进入每个允许location，不能只写在父级。
依据[Nginx location](https://nginx.org/en/docs/http/ngx_http_core_module.html#location)、
[Real-IP原始地址](https://nginx.org/en/docs/http/ngx_http_realip_module.html#var_realip_remote_addr)及
[请求头转发](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass_request_headers)。
要求Nginx带Real-IP模块；前置CDN/负载均衡只显露代理IP时不得把代理整个加入来源名单，需另定接入合同。

本片生成固定合并制品，不自动修改宿主。目标模块配置、正式入口安装/回退、授权工具往返和业务验收仍须逐项完成。
