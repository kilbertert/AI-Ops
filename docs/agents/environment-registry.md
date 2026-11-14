# 业务环境与租户接入登记

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
95启用登记；未设置的新选项不改变旧41。首次启用需重启配套合并制品，后续租户启停无须重启。
Gateway用户准入、控制台新登录/令牌获取/身份查询、自助开通目标与受控工具重新授权均校验登记。
客户端业务端点由登记决定，不再使用旧CPMS默认清单推导未登记租户。

停用只表示下一次准入拒绝，不撤销已完成回答，也不能撤回已经发送的上游请求。
Dify已签会话仍受原寿命约束；登记不声称全局清除共享Dify的所有浏览权限。
环境地址/配置变化不是租户迁移快捷方式，须准备完整配置并重新部署绑定；历史会话仍属于原环境。

## 95重建的操作约束

通过[受管访问](env-95-facts.md)盘点aiops-gateway、两个既有/v1反代、数据根和实际访问日志。
日志仅在主机内汇总路径/状态/时间段，不输出用户令牌/查询串。先做私有备份并验证恢复资料，
再将合并制品安装到独立目录和新数据根，核服务身份及健康，最后切换原入口、退休旧运行文件。
公司MySQL/Redis/TDengine及非AI-Ops反代不属于清理对象。旧资产备份从运行目录隔离，不再参与新实例。
没有真实认证/业务样例时只报告基础设施与合同验证，不能报告三个业务功能验收通过。
