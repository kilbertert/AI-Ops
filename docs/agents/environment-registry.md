# 业务环境与租户接入登记

> 下文是现有隔离实例的登记实现。95试验已撤回，新目标见[公司网关方案](../architecture/2026-11-14-company-gateway-onboarding.md)。
> 现有JSON/CLI不是集中管理页面；每环境独立Gateway也不表示必须部署在客户服务器。

公司认证负责用户/权限，AI-Ops维护环境与租户开通关系；采用每环境独立Gateway、登录代理、数据根。
首轮由平台运维维护私有JSON，不逐个登记用户、不让客户端提交服务URL。决策见[ADR0256](../adr/0256-platform-environment-registry.md)。

## 登记与发布

输入是环境数组，各条含id、enabled、gateway_url、cpms_base_url、config_ref、tenants；
tenants为id/enabled数组。id使用规范环境编号；config_ref是该环境运行配置文件绝对路径，
配置正文及凭据仍存受控位置。端点远端HTTPS、本机回环可HTTP，不带凭据/query/fragment。
环境可共用物理机，不可共用Gateway入口；同租户编号可在不同环境独立登记。

```bash
# input.json与目录为0600/0700；不要将现场数据放入Git。
aiops-admin environment-registry apply --registry /var/lib/aiops-registry/registry.json \
  --file /var/lib/aiops-registry/input.json --expected-revision 0 --by operator
aiops-admin environment-registry check --registry /var/lib/aiops-registry/registry.json
```

每次修改携带当前预期版本；过期写者被拒。修改通过同一apply发布，保留递增版本及操作人/时间。
check仅打印版本和计数，不打印地址、配置引用或秘密。备份旧私有文件后再发布，回退也按新版本发布旧内容。
多宿主分发由受控部署传递同一版本，运行时副本不可各自编辑；未回读目标版本的宿主不算生效。
入口渲染使用`render_business_ingress.py --registry <登记文件>`，独立凭据文件只保存环境id与密钥；地址只来自登记。

## 生效与范围

Gateway配置AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE，登记config_ref必须与server_config_file一致，
execution_environment与environment一致；登录代理配置AIOPS_LOGIN_PROXY_ENVIRONMENT_REGISTRY_FILE。
95试验期间启用登记，现已撤回；未设置的新选项不改变旧41。首次启用需重启配套合并制品，后续租户启停无须重启。
共享知识服务按环境和公司租户共同识别账号，贯穿开户、检索/媒体、发布检查及运维命令；
本地账本与公司API仍使用原公司租户ID。旧实例启用登记前需显式迁移知识账号，不能只切开关。
Gateway用户准入、控制台新登录/令牌获取/身份查询、自助开通目标与受控工具重新授权均校验登记。
客户端业务端点由登记决定，不再使用旧CPMS默认清单推导未登记租户。

停用只表示下一次准入拒绝，不撤销已完成回答，也不能撤回已经发送的上游请求。
Dify已签会话仍受原寿命约束；登记不声称全局清除共享Dify的所有浏览权限。
环境地址/配置变化不是租户迁移快捷方式，须准备完整配置并重新部署绑定；历史会话仍属于原环境。

## 95历史重建操作约束

以下仅为历史试验与恢复资料；95已撤回，任何恢复须独立决定，不是当前部署计划。

通过[受管访问](env-95-facts.md)盘点aiops-gateway、两个既有/v1反代、数据根和实际访问日志。
日志仅在主机内汇总路径/状态/时间段，不输出用户令牌/查询串。先做私有备份并验证恢复资料，
再将合并制品安装到独立目录和新数据根，核服务身份及健康，最后切换原入口、退休旧运行文件。
公司MySQL/Redis/TDengine及非AI-Ops反代不属于清理对象。旧资产备份从运行目录隔离，不再参与新实例。
没有真实认证/业务样例时只报告基础设施与合同验证，不能报告三个业务功能验收通过。

## 管理目录schema2与原Dify页面

先部署兼容schema2的Gateway/管理桥，再迁移目录；旧运行程序不认识新格式，不能直接用旧制品恢复schema2。
`migrate`在同一目录锁内显式备份schema1并递增版本，名称初值沿用编号，公司网关地址为空，管理员授权为空，不自动扩大权限。

```bash
aiops-admin environment-registry migrate --registry /var/lib/aiops-registry/registry.json \
  --expected-revision 1 --by operator
```

新`apply --file`接受`{"environments": [...], "managers": [...]}`；旧数组输入只用于schema1，拒绝覆盖已升级目录。
环境在旧字段之外必填`name`和`company_gateway_url`，后者为空表示尚未登记，非空须HTTPS，可含公司路径前缀。
它不替代现有`gateway_url`（隔离AI入口）或`cpms_base_url`（现役工具入口），本片不据该新字段发请求。
租户增加`name`。管理员记录为`environment/tenant_id/user_id/environments`，前三项是正常登录主体，最后是可管理环境ID数组。
schema1不能直接apply新格式；必须migrate，迁移中断后可复用同内容且私有的备份重试，冲突备份明确拒绝。
管理员列表只由受管CLI配置，不从公司type=-1自动推导，也不能从页面编辑。实际身份与地址保存在私有文件，不入仓。

Dify原AI管理页增加“环境与租户管理”：读取获授权环境，修改环境或已有租户名称、准入开关；
每次写入携带读取版本，冲突先刷新。目录列表不返回地址/配置路径/管理员/秘密。
API为`GET/POST /v1/dify/management/environments`，经现有同源桥的`environments/environment-update`动作，
保留Origin、CSRF、Dify角色、服务身份和正常公司会话验证；不提供任意URL、SQL或主机执行。

本片只管理登记与准入，所有新接入仍标“待验收”；不创建新环境运行服务、自动发现租户或撤销所有既有Dify会话。
目录管理员的独立授权可在业务准入停用后继续操作；公司会话失效仍拒绝。登录阶段的业务停用限制未迁移，
因此若当前登录环境也被停用且会话随后过期，需要受管CLI恢复；不要把唯一控制环境作为试验停用目标。
新租户由公司目录核权后受管登记，真实发现及跨环境开通留后续片；不得把手工填写ID当业务授权。
回退目录内容仍走递增版本发布；保留任务备份，不能用旧schema1数组静默丢掉新授权。

目录修改使用全局revision，两个环境同时保存也可能冲突，需刷新；未将目录并发扩展为分环境版本。
环境运维不要求公司智能体编辑/发布权限，但正常公司身份和显式范围授权缺一不可；应用内容权限不因此放宽。
