# 归档内容预检交付与控制Gateway部署

Refs #1535、#1540。实现#1578合并修订 `816ea87aa2387ee8f50e129a2ad1f716eaa85970`，版本1.17.0。
最终CI `44230315480`：5257通过/10跳过；Workflow policy通过，Devin发现已复现修复、答复并请求复审，线程闭环。
本片为归档内容预检，不开放远程执行或激活，P4/P5仍未完成。

应用包成员校验与P3安装器共用gateway.environment_artifacts；安装器携同修订固定副本，
冷安装不依赖预装Gateway。计划新增application-contents、installer-contents和embedded-revision：
成员/内部摘要、目标Python及包内声明修订分别判断，读取归档不执行其中代码。
所有计划仍blocked/executable=false；不证明Git合并来源、wheel代码身份、配置投影或现场可用。

独立首轮发现新安装器入口未同步文件集合、截断gzip的EOFError；均复现修复。
后续中段DEFLATE反例在旧候选仍抛zlib.error；修复77dc73c9后，独立使用两份完全相同的原损坏字节重放，
对应contents=blocked、新计划可读且不可执行、旧计划expired，生效目录字节保持。
18项原入口/5项格式检查与中段修复复验分别留证；备用Codex的旧候选不通过报告也保留，不能冒充新候选通过。
未执行反例便终止的首选DeepSeek进程不计验收；独立会话具体模型ID未暴露，报告不按请求别名猜模型。
原#1577修复提交缺逐提交ADR说明而被流程门禁拒绝，未强推或修改门禁；#1578从主线重新组织提交，
原PR关闭为被替代，原分支和bundle保留。

作者原候选全量5252通过/9跳过不代替最终CI；修复后99项组合及最后69项相关检查通过。
全仓Ruff/格式、compileall、uv pip check、wheel/sdist及既有格式下的Oxfmt通过。
一次性Gateway真实启动/health/鉴权/OpenAPI/退役入口通过，不能证明远端模型、知识或业务。
保留的旧P3应用包和五文件安装器通过新归档规则；合并修订的新安装器包在无site-packages解释器冷加载通过。
实际服务宿主的install/recover未在本事件重跑，不由替身或冷加载推导通过。

41标准CD `44231406694` 的deploy-41、release-notes均成功；180个源码逐SHA一致，
health回读 `1.17.0+816ea87aa238`，注释tag `v1.17.0` 指向合并SHA。
部署前零在飞run、近期无发布；前后草稿、正式路由、生效目录、计划行数1及六共享容器均保持。
引用配置仍为空、生效环境0。第二环境接入实例未随之升级；未改公司服务、业务行、网络暴露或生产引用配置。

36完整Web从同合并修订固定上游webpack构建成功；外层归档与config blob摘要核对后仅切换DIFY_WEB_IMAGE并重建web。
运行内容摘要 `sha256:6760de47efdc386616078d25c8eea1153c8bc811ac22105a1e1eb12b97470ac4`，
环境页路由及新增归档分项标识存在于编译镜像，其他容器ID保持，旧镜像和配置/元数据保留。
首次编译内容扫描将find/grep的部分未命中退出码当整批失败，检查器订正为判实际匹配输出后通过；未改镜像或生产行为。
正式TLS的signin、环境页、工作室和知识页回归通过，匿名页面只证明登录重定向。

2026-11-18 03:49 UTC，原TLS域名上正常公司登录200，验证码由用户人工输入；原环境页/新增环境入口可见。
管理读取200、can_create=true、registry_revision=1、effective=0、drafts=1。
已有草稿重复预检/编号回读均200且同计划，页面“开通计划”可见；references=blocked、现场项pending、executable=false。
原生退出200/result=success；旧会话allowed=false、SESSION_LOGIN_REQUIRED且无公司身份。
本次未配置真实制品引用，因此现场不验证归档成功分项；其格式边界仅由上述隔离证据证明。
前后草稿、目录、正式路由和六共享容器保持，仅新增一条自有计划；不激活、开户或发布应用。
临时浏览器和运维连接已结束；[前一片验收](2026-11-18-environment-plans-live.md)仍保留其原版本范围。

本片操作复用[环境目录手册](../agents/environment-registry.md)、
[P3安装手册](../../ops/environments/gateway/install.md)；原始反例、模型回执、构建、快照、制品和恢复账本保持私有。
源码回退保持原草稿/计划格式，安装器恢复使用其同版完整包，不倒写目录版本或删除业务状态。
下一步仍为配置投影、现场预检、持久执行/恢复、资源准备和激活，随后双环境三功能业务验收。
