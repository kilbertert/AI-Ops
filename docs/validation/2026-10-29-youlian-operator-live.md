# 优链充管家部署与 App 验收

本记录替代[准备回执](2026-10-29-youlian-operator-prepare.md)中的待登记/待发布状态。
PR #1136合并`5960526bc4fd4434ec2978ee54df2352de7d1fa3`；最终CI3318 passed / 10 skipped，
policy/spec通过；Devin首审3条及复审1条均处理，线程已关闭。Codex自动审查因额度未完成，
不计为审查通过。本文无运行时逻辑变更。

## 已部署

- 合并环境清单SHA-256：`0720a3a968a9094701e3d1ccc36814698057a8ba91360e01278156ffe0d62d7f`。
  dev-host传输核验后更新现场清单；仅追加一个operator节点目标。
- 定点创建并发布空旧工具初始版本V1；只收敛新租户的应用登记，两条原登记unchanged。
- 2026-10-29 12:20:27（北京时间）正式发布200，node-sop V2；
  `production-41 / operator_order_diagnosis / node / active / revision1`。
- 重复发布200 unchanged/V2。新应用UUID `349a637c-8f86-41b9-b932-e9818fb39a97`；
  Gateway agent `agt_66e77858b34f476ca171fbb2984e97be`；原租户三条路由与源model_config不变。
- 无Dify、Java或Gateway重启，无新增暴露面、公司权限修改或业务写操作。

## 第一次真实 App：正例未通过，空范围拒绝已复现

用户授权代理通过其提供的手机号验证码正常登录。Dify密码登录本轮由用户选择暂缓，
本轮验收范围改为管家 App，不能将App结果写成Dify原生预览结果。

- 正常公司登录HTTP200，UPMS `/user/info`确认目标租户；账号类型5。
- 原首页进入智能检测，原选择器可选订单，发送后HTTP403
  `OPERATOR_SITE_SCOPE_REQUIRED`；页面显示拒绝，无脚本错误。
- 独立权限目录：UPMS店铺0、映射站点0；按代理商B账号和partner实体查站点归属也为0。
- 公司订单列表200且总数694，独立只读SQL核租户订单总数同为694；列表样本归其他站点主体。
  因此列表可见不能证明当前账号有诊断站点授权。
- 对应测试订单的节点run数0；拒绝发生在派发与诊断取证之前。
- 公司源码查证通过仓库规定的只读GitLab API：项目363/release `ac3de27b`，2013个Java文件。
  StaffSiteScopeService只对51类型施加新员工范围，当前5类型沿用旧拦截器；
  旧ShopIdInterceptor存在空集合时不添加范围条件的路径。源码与实测共同说明两侧授权口径不同，
  未将源码HEAD等同于线上制品，未擅自放宽AI-Ops授权或修改公司仓。

用户已选择更换有权限的测试账号；新账号待提供。成功取数、双向合法跨租户订单/QA拒绝仍待验证。
**未完成业务验收**；历史故障准确率不在本次自动化与配置验收结论内。

## 证据与恢复

私有证据保存本次登录、浏览器storage、截图、POST响应、目录查询、源码片段、路由快照和SQLite在线备份。
不得把手机号、验证码、token、完整订单或人员信息复制到公开PR。原生进程检查使用一次性数据根。
原账号403不触发撤回新租户配置；两租户active路由仍在位。恢复按新租户retract执行，原租户保持不变。
方法已入[多租户手册](../agents/operator-cpms/multitenant.md)，并随目录迁移修正publication包导入。
