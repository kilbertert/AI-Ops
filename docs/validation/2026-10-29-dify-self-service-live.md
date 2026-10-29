# Dify自助开通部署回执

PR [#1147](https://github.com/kilbertert/AI-Ops/pull/1147) 已合并`b86be95f1831e24a5611ba300aeac7aa29b0b625`。
CD `40835707392`成功，Gateway健康版本`1.0.0+b86be95f1831`，business_mutations=disabled。
必需CI3344 passed / 10 skipped；本地3345 passed / 9 skipped。Devin与Codex自动审查均因额度
不足未完成完整审查；无待解决审查线程，不把服务的pass占位当作审查通过。

## 同版制品与部署

- Gateway：CD从合并修订部署；首次profile配置后健康检查恢复成功。
- Dify API/worker：新增确定性导入只读挂载；7个候选文件的6个旧文件与现役逐字相同。
  原生`GET /console/api/apps/imports`真实回读`aiops_idempotent_import=1`。
- Dify web：合并内容重新Webpack构建，在开发机组装镜像`aiops/dify-web-patched:self-service-b86be95f`，
  image ID `sha256:c9526c99924867a26f680b26d54873e52544b90c45ff20844f2632d939a61c65`。
  镜像传输sha256 `76b6dd248630525829f21ca8de03f7ccc7d76261eea5f0ef89d4218f9983f24f`，36加载后定点重建web。
- 发布桥：合并制品sha256 `f40888332ac6a84a7b2b454c1c0e1a593d578902ece3694f957de063fa24112b`，
  安装脚本校验、模块路径更新、nginx校验及匿名管理入口403均通过。
- 登录代理：完整合并Python包替换，服务单元改为`aiops_diagnostics.dify.dify_login_server`；
  服务身份下导入通过，启动active。已有公司令牌缓存清空，需正常重新登录。
- 平台profile：现场读取现役连接和Dify模型能力，凭据在服务身份0600文件，应用只引用`operator-41`。
  已登记两个既有管家，未重建工作区/app、未重新发布。

## 线上非业务核对

全部4条正式路由部署前后逐字段相等；其中原管家V22/revision3/active，优链充V2/revision1/active。
公网登录页真实Chromium打开、3个输入框、页面错误0；未登录管理请求被拒绝。
Dify API、web、桥、登录代理服务正常。现有nginx重复MIME告警仍在，没有改动无关配置。

**未完成业务验收**：已通知用户用平台正常登录，在Dify对优链充创建“优链充－自助开通验收”草稿。
真实平台可选客户目录、UI端到端开通结果仍待该次操作确认；优链充成功订单诊断仍需要有站点权限账号。
本次没有用管理密钥伪造公司用户或扩大站点范围；历史管家间歇完成失败不在本回执中宣称已修复。

部署与恢复方法见[自助开通手册](../agents/operator-cpms/self-service.md)。
原始日志/路由对照/截图在私有`~/.cache/aiops-dify-self-service-research/`。
