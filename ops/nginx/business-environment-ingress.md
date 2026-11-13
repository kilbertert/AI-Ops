# 可信业务环境入口

第一片复用nginx把请求送到各环境独立Gateway；每环境仍有独立登录代理、配置和数据根，共享AI业务合同与Dify。
生成器不读JWT、租户或域名目录，也不会探测任意地址。平台接入目录是配置，不是公司目录发现已实现。

## 受控目录与生成

私有JSON（0600，同服务账号）使用schema `aiops-business-ingress/1`、environments数组，每项：

| 字段 | 含义 |
|---|---|
| id | 已登记的规范业务环境ID |
| gateway_url | 该环境Gateway根地址，不能是CPMS路径；各环境独立地址 |
| ingress_key | 来源代理专属随机凭据，64位小写hex；不能发给客户端 |
| company_source_key | 对应Gateway现有来源密钥，16至256位字母/数字/下划线/连字符；接入面覆盖注入 |

凭据运行时从受控位置读取，不在示例中填实值。每环境生成独立随机值，重复/非法记录整体拒绝。
不轮换既有来源密钥；目录只登记已验证的原值，不把入口密钥当成用户身份。

```bash
uv run python tools/content/render_business_ingress.py \
  --catalogue <私有接入目录.json> --listen 127.0.0.1:<已分配端口> \
  --trusted-ca <上游可信CA文件> --out <私有nginx片段.conf>
```

片段放现有nginx的http块（包含maps、upstreams和server）；监听只能loopback。TLS/public入口仍归已批准反向代理，
本次不开放公网端口。远端上游必须HTTPS，证书CA与主机名始终验证；HTTP仅允许回环IP。
启动前先确认上游health/部署身份和租户归属确实对应id；渲染器不能根据URL证明业务归属。

可信公司入口必须覆盖客户端传入的`X-AIOps-Environment-Key`，注入该入口自己的凭据。
不要把此密钥放App代码、URL、JWT或浏览器配置。接入面按凭据选择环境，并覆盖环境和上游source key，
Authorization保持原样：**来源凭据只证明代理路径，用户身份仍由该环境Gateway验证**。
客户端任意环境头/JWT claim不能改变上游。缺失、未知或重复凭据拒绝；没有默认目标。

## 安装、验证和回退

1. 获取合并制品并备份当前http配置；目录和渲染结果均保持私有，不在公开CI/聊天输出。
2. 以nginx服务身份校验，核临时目录属主；方法见[离线接入手册](../../docs/agents/operator-cpms/device-offline.md)。
   配置中可能含密钥，`nginx -T`输出不得写入公共日志。禁止以root校验后遗留错误目录属主。
3. 审查通过后以现有宿主安装流程加载。入口移向新接入面属于单独受控部署，须有原入口流量证据和回退点。
4. 各环境正常身份验证、同ID不同数据、未知来源、撤销/断连逐项验收；不能用匿名health代替真实用户问题。
5. 回退原入口及配置，保留各Gateway、原数据库和共享Dify空间；不为了切流量删除旧实例。

连接失败只返回该环境错误，`proxy_next_upstream off`禁止把用户令牌转到另一环境尝试。
目录移除环境后须重新生成并加载配置；渲染文件存在不意味着变更已生效，既有在途请求仍可能结束。
本片只承接`/v1/`业务面；控制台登录/验证码/预览发布路由是后继接入范围，未声称已集中支持。

## 证据边界

[验收计划](../../docs/acceptance/business-environment-ingress-qa-plan.md)用真实nginx、临时服务与一次性Gateway。
真实业务环境的CPMS/时序/离线源、中心目录有效记录、跨域控制台会话和原App问题仍待核实。
