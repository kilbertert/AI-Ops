# 环境开通前验证

适用任务：#1536（P0）。本手册只规定只读核查和隔离验证；不授权生产安装、修改公司服务或开放入口。
预检分别报告身份、网络、资源和业务能力，不能用一个总体健康状态替代。

## 1. 身份与入口

先从私有清单确认目标主机身份、角色、所有者和访问方法；使用`dev-host`及项目声明访问。
只对明确获准的地址探测，不拿一个环境的登录凭据遍历其他环境。
正常登录必须由绑定环境的登录代理亲自完成，或在该环境现有会话源中验证消费者会话。
原App仅提供旧共享签名JWT时，单独记录兼容性，不能仅凭同号用户存在而交换AI会话。

本地检查：

```bash
uv run pytest tests/dify/test_dify_login_proxy.py \
  tests/integrations/test_business_environment_ingress.py \
  tests/gateway/test_environment_registry.py tests/operations/test_operator_token_ingress.py
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/onboarding
```

新增双代理用例故意共享公司用户/租户编号、公司令牌和Dify签名密钥，保留独立环境映射/会话存储。
必须证明本环境登录成功、跨环境交换拒绝、未绑定会话拒绝、退出只影响原环境。
这不验证真实公司账号或整个Dify浏览权限，也不证明旧App凭据可以直接支持跨环境。
`publish/check`与`operator-token`共享正常登录的活动绑定；本片同时验证两个入口在退出/跨环境后的拒绝。
权限码及工作区成员仍是既有快照；会话有效性一致不证明公司侧任意撤权已实时刷新。
原生退出路径必须配套部署登录代理snippet，否则原按钮无法撤销绑定。

## 2. 正式网络逐方向验证

接入模块→共享执行服务、共享执行服务→接入工具、管理服务→工作区/知识接口分别核查。
既有HTTPS虚拟主机的工具回调生成与安装边界见[扩展入口](../../ops/environments/gateway/ingress.md)。
在实际调用方运行；开发机成功不代表目标服务器成功。SSH只用于受管管理，不充当线上业务通道。
先匿名核TLS与拒绝响应，再用获准凭据做真实接口验证；后者的凭据不得交给下面的匿名探针。

受管检查清单放库外私有文件，模式0600；示例中域名需替换成已批准的端点，不含真实地址或秘密：

```json
[
  {"id":"shared-auth","url":"https://shared.example/v1/info","status":401},
  {"id":"gateway-health","url":"https://gateway.example/health","status":200,
   "json_equals":{"ok":true,"service":"aiops-gateway","business_mutations":"disabled"}}
]
```

```bash
uv run python tools/verification/business-environments/network_preflight.py \
  --manifest /private/approved-network-checks.json --attempts 3 --timeout 5
```

这是人工运维验证工具，不是面向用户的任意URL探测API；其输入须先经过目标审批。
`json_equals`仅支持顶层字符串、布尔、整数及null字段，不接受嵌套结构或浮点近似比较。
默认验证TLS、不跟随重定向、不继承环境代理；结果只含逻辑检查名、状态和异常类别，不带URL、响应正文或秘密。
本工具证明直连路径；需要代理的正式运行路径须另行从实际应用验证，不能把本工具绿灯当作代理链验收。
全部样本匹配才返回0；任一失败返回1；私有清单错误返回2。200必须结合服务响应字段判断，不能把登录页当目标API。
`timeout`是单次socket操作超时，不是整个任务的绝对时限；受管远程运行同时设置`dev-host --timeout`总时限。
该脚本需要本项目Python依赖；不能为运行预检在生产安装开发环境或上传未合并代码。

TLS被重置时，核DNS、TCP、带正确SNI的证书握手，再核HTTP；必要时在两端同期短时捕获TCP包头，
仅限目标地址/端口，不采业务载荷。对齐连接四元组、序号和方向：单端看到RST不能定位来源；
两端记录能进一步区分本机关闭与路径注入，但仍不能直接认定某厂商或某台设备。
一次成功和多次失败同时保留；不禁用证书、不改明文、不恢复专属隧道。

## 3. 容量与既有服务

按目标声明使用`dev-host exec <目标> --allow-service-exec --timeout <秒数> -- <只读命令>`。
只读样本使用`free -m`、`vmstat 1 3`、`df -Pm /`、`systemctl show <自有服务> -p MemoryCurrent -p MemoryPeak`
和限定自有容器的`docker stats --no-stream`；不用完整进程环境/命令行或日志作为公开回执。
真实健康接口只GET；公司进程的当前容器状态不等同业务接口成功。

用[通用候选预演](../../ops/environments/gateway/README.md)从合并修订构建目标Python离线包，在一次性数据根测RSS/HWM、健康请求并发和双实例重叠；共享Java节点另计。
健康请求负载只证明启动成本，不能代替模型调用、订单/离线查询与知识检索并发预算。
swap存量与短时换页分开解释；目标长期容量未证明就保留缺口，不为了压测挤占公司业务资源。
若目标已有AI-Ops旧部署，先记录版本、所有权和实际访问，再决定切换；不因版本旧而自动停止。

候选预演前后分别保存既有Gateway的`MainPID`、`ActiveEnterTimestampMonotonic`及health，
对已登记宿主的运行容器只投影`Id`与`State.StartedAt`，原始快照留在私有运维记录。
身份不变但启动时间变化属于异常观察，不能直接断言由预演造成或与预演无关；
只读核对该容器的`RestartCount`、`State.OOMKilled`和有界`docker events`退出事件。
事件查询的`--until`取当前或过去时间，避免为了读取历史而等待未来事件；不为取证重启公司服务。

预演结束核`systemctl list-units --all --no-legend 'aiops-candidate-*'`及本次候选根均为空。
上传暂存文件先逐个核对本次摘要再删除，空父目录用`rmdir`；不要按宽泛名称批量清理。
完整制品、probe摘要、环境、UTC时间、结果与异常记录进入私有部署账本；回执只保留脱敏结论。

## 4. 继续条件

P0只有在身份、两个方向正式网络和所需容量证据齐备后才完成。缺账号、网络或授权时保留任务，
本地检查和匿名连通结果可独立交付，但不能据此启用真实租户。
后续目录/页面必须沿用上述拒绝条件，不能用配置默认值、伪造心跳或合成订单补齐缺失验收。
