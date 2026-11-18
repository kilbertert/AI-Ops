# 开通文件预检计划部署与正式页面验收

Refs #1535、#1540。实现#1575合并修订 `f0b016ba01511604f64e15fe3e8ebf25e38fc846`，版本1.16.0。
CI `44157738280`：5249通过/10跳过；流程检查通过，独立反例修复及Devin审查闭环。
本事件覆盖控制Gateway、管理桥、完整Web配套升级和原单环境管理入口；P4远程执行与P5业务验收未完成。

41标准CD `44158696174` 的deploy-41、release-notes均成功，health回读 `1.16.0+f0b016ba0151`，
179个源码逐SHA一致；注释tag `v1.16.0` 指向同一合并提交。部署前零在飞run、近期无发布，
正式路由、生效目录及六共享容器保持。第二环境接入Gateway未随本次升级，登录代理保持已验收的1.15.0协议。

36管理桥来自同一合并完整制品，经既有deploy.sh安装并active。Web执行完整固定上游webpack构建，
归档SHA和manifest指定的config blob摘要匹配后加载，仅切换DIFY_WEB_IMAGE并重建web。
运行image config摘要为 `sha256:3e8e9cf770a79ff0427d3da10d119791f11f3257a094b376ad2a9cd5d49717d0`，
镜像内路由清单包含环境页，其他容器ID全部保持。旧镜像、配置及元数据保留作回退。
归档尚未传输完成时的一次部署探针摘要拒绝，未切配置或服务；传输完成后同制品部署通过，原失败保留。
正式TLS的signin、环境页、工作室和知识页检查通过；未登录页面重定向到登录页，只证明入口保持。

2026-11-18，经原TLS域名与受管临时运维连接，验证码由用户人工输入，完成正常公司登录：

- 登录200；原环境管理页面、“新增环境”和已有第二环境草稿可见。
- 管理读取200、can_create=true、registry_revision=1、effective=0、drafts=1。
- 已保存草稿预检两次及编号回读均200，复用同一计划；页面点击“配置与制品预检”后显示“开通计划”。
- 受控引用尚未配置，references=blocked；配置投影、主机身份、容量、网络和业务检查均pending。
  scope=local-file-integrity-only、executable=false，页面没有把文件预检当开通成功。
- 原生退出200/result=success；旧会话allowed=false、SESSION_LOGIN_REQUIRED且无公司身份。

前后只读快照：草稿、生效目录、正式路由、health及六共享容器摘要相同；仅自有计划表从0行增加为1行。
未保存草稿、激活目录、开户或发布应用，未修改公司代码、业务行或服务；临时浏览器和运维连接结束清理。

恢复及操作复用[环境目录手册](../agents/environment-registry.md#Web发布后核验与镜像摘要)、
[登录手册](../agents/dify-login-proxy-36-runbook.md)和[管理桥部署](../../ops/dify-console-bridge/README.md)。
原始图像、HTTP、前后快照、归档摘要、组件备份和部署账本保持私有。
[开发验证](2026-11-17-environment-onboarding-plans.md)与本回执各承载独立验证事件，不再增加重复进度页。

本次未验证远程主机/制品内部及合并来源、配置投影、安装执行与恢复、资源准备和激活。
第二环境正常登录方式、真实工作区/应用、合法run、双环境三功能和长期容量仍待完成。
