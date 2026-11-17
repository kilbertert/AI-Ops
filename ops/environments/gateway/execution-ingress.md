# 共享执行平台HTTPS入口

目标接入模块沿现役`SnailDispatcher`通过正式HTTPS派发任务。本方法补目标→共享平台方向；
反向工具调用使用[工具入口](ingress.md)。本片不注册执行组、不部署节点、不激活租户。
合同见[ADR0274](../../../docs/adr/0274-bounded-shared-execution-ingress.md)，验收为环境目录QA的D16。

## 制品与前置核查

先核共享宿主、已有域名/TLS与扩展目录，记录原配置摘要、流量及恢复点。确认`/snail-ai/`
没有现用路径；观察不到时按在用处理，不覆盖。上游为已登记平台的固定内部IPv4与HTTPS端口，
CA文件只含公开证书，证书SAN必须匹配所传TLS名称。Nginx需Real-IP模块，来源是实际连接方；
前置代理场景不得把公共代理IP或自报XFF当环境身份。

核现役宿主在请求解析前的日志设置：不得记录`Snail-Ai-Token`、Authorization或请求正文，
错误日志不使用info/debug，未启用debug_connection。片段关闭自己的日志，不能保证宿主所有
畸形请求日志都脱敏；任何秘密不得放URL。公司现有日志不合格时另行协调，不擅自改动。

```bash
bash ops/environments/gateway/build-execution-ingress.sh <合并SHA> /private/execution-ingress.tar.gz \
  https://<平台内部IPv4>:<端口> <证书DNS名称> /private/public-ca.crt <目标环境来源IPv4>
```

开发机从合并修订归档源码生成包，拒绝覆盖已有输出；包内只有REVISION、SHA256SUMS和
`aiops-execution-ingress.conf`。地址、来源与CA位置仅是私有部署参数，不进入仓库。
上游只接受RFC1918或回环IPv4，拒绝链路本地/保留地址、公网、HTTP、动态域名及带路径/凭据的URL；
回环用于平台有受管本机映射或隔离验证场景，具体端点必须对照主机清单，生成器不替代部署授权。
来源只接受1至8个不同的IPv4。

## 安装与恢复

具体虚拟主机扩展与reload取得对应授权后，用`dev-host cp --artifact-sha256`传固定包。
不改平台或公司服务，不开放新端口；以下由宿主维护身份执行：

1. 核外层/内层摘要、CA可读性与平台TLS握手；保存旧扩展（初次缺失也记录）及现役公司路由摘要。
2. 先以宿主实际Nginx二进制执行`-t`。仅把本次片段装入既有server扩展目录，root所有且不可被组/其他用户写。
3. 再次`nginx -t`；失败恢复原片段且不reload，成功才reload。等待新worker实际接管请求，不能只看命令退出码。
4. 从获准目标验证缺凭据/错凭据拒绝，正确应用请求交给平台；错误方法405、管理路径404、HTTP403。
   从非获准来源带伪造XFF仍403；比对原公司路径响应和平台/节点容器身份。
5. 失败或撤回：先核本次片段摘要未被其他任务改动，再只恢复该文件（原缺失则删除），`-t`及reload后复验原入口。

应用凭据只从受控文件在进程内读取，不进入命令行、URL、日志或回执。代理透传应用ID与token，
不传Cookie/公司身份/平台管理员头；实际鉴权仍由平台执行。缺凭据401由入口拒绝，错凭据的
具体HTTP/信封语义以真实平台为准，不把HTTP200单独作为成功。请求体上限64KiB，URI必须规范且无query。
片段的server层规则在location选择前拒绝携Snail应用头的其他URI，避免点段穿越后把凭据送到公司路径；
公司请求不得携Snail专属头。部署前核本虚拟主机没有其他Snail调用方，并检查现有server层rewrite无提前return/改写冲突。
共享平台管理API通过受管内部操作使用，不因资源注册而扩大公网白名单。

```bash
uv run pytest tests/integrations/test_execution_ingress.py tests/integrations/test_environment_tool_ingress.py
```

本地双段TLS替身证明代理合同；完整合法run、身份隔离、模型与业务结论必须通过真实链另行验收。
