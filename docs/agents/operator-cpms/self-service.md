# Dify内开通与管理客户管家

平台运营在应用列表点击“复制并开通客户管家”，选择来源正式版本、公司客户和应用名称。
系统准备工作区、模型和工具、登记应用、首次发布；结果和失败步骤在同页显示。
刷新后仍能查原任务，重试不会重复建工作区或应用。已有正式管家的客户仅新增草稿。

租户正常登录自己的workspace，在编排页看到实际Java模型、预算和版本。预览读取当前编辑，
“发布到管家”先保存、核对摘要再切正式版；历史版本操作提供撤回/恢复。没有场站权限的账号
会看到明确提示，不会因为开通配置而自动获得订单访问权。首片沿用现有租户内编辑/发布政策。

## 平台首次准备

代码经PR/CI合并，Gateway由现有CD部署。Dify API导入补丁、前端镜像和同源桥应同版安装。
平台运行配置通过 `AIOPS_GATEWAY_OPERATOR_PROFILES_FILE` 指向0600文件，格式为：

```json
{"schema":"operator-profiles/1","profiles":[{"id":"default","name":"标准订单管家","target":{"environment":"production-41","entry":"operator","config_file":"<现役节点平台配置路径>","manifest_file":"<现役operator清单路径>","cpms_base_url":"<已批准CPMS HTTPS入口>","proxy_base_url":"<现役节点回调HTTPS入口>"},"model":{"plugin":"<已安装且有包的精确插件标识>","provider":"<Dify模型provider>","name":"<模型名称>","completion_params":{"max_tokens":4096,"temperature":0.3},"credential_file":"<平台0600模型凭据JSON路径>"}}]}
```

凭据JSON使用Dify原生模型配置字段：api_key、endpoint_url、mode、context_size、
max_tokens_to_sample、function_calling_type=tool_call、stream_function_calling=supported等。
运行时读取，不写进仓库。模型名称满足Gateway当前模型白名单；Java模型仍由执行清单决定，
页面显示该真实值。新客户不能提供自己的endpoint或密钥覆盖平台模板。

平台用户的可选客户来自公司 `/upms/tenant/list/my`，该接口按sys_user_tenant关联查询；
需要使用正常平台Dify登录，并具有公司aiops:agent:edit和aiops:agent:publish。
目录为空时应由公司管理员管理账号的客户绑定，不能改用返回全租户的接口放宽范围。

## 部署与验证

1. 读取当前交付状态与41手册；备份现役源码/桥/前端镜像/配置及数据库在线副本。
2. 用合并制品运行Dify `patches/apply.sh`，核固定原文件摘要；新增app_import只读挂载，
   由 `GET /console/api/apps/imports` 回答 `aiops_idempotent_import=1` 证明能力。
3. 构建web-patches镜像；部署已有Dify web服务，保留旧镜像。控制台原生普通应用仍按原行为验收。
4. 用同一合并修订构建并安装console bridge包；新增 `/__aiops/manage` 必须同源会话/CSRF检查。
5. 服务身份写入平台profile配置，Gateway启动时校验连接与凭据位置并登记现有受管应用身份。
   必须比较旧app、provider、正式版本、revision不变；之后客户开通完全走页面，不再改文件。
6. 平台正常登录，先核目录与来源，再在页面开通目标；按操作ID回读complete。
   租户正常登录原App选明确订单，核Java succeeded/matched、双CPMS与原页面报告。

读API：management/catalogue、management/operations/{id}；写API：operations、retry、publish、version；
所有路径位于 `/v1/dify/management/`，发布服务key与公司Dify登录会话缺一不可。
浏览器只调用固定动作的同源manage端点，拿不到内部token和平台密钥。

## 恢复

失败任务保留资源ID与步骤，正常登录后点击重试；重启不会丢失进度。若Dify创建响应丢失，
确定性request_id回读同一app。撤回只停新结果交付，不删除应用/工作区/审计。
部署恢复按原入口分别恢复旧前端镜像、桥和Gateway制品；不把数据库回滚到旧副本覆盖业务记录。
新表可由旧版本忽略，但恢复前要确认没有新受管应用仍需新版目标解析；需要时先撤回该新入口。

场景与步骤见 [验收契约](../../acceptance/dify-self-service/acceptance.feature) 和
[QA计划](../../acceptance/dify-self-service/qa-plan.md)。本地合成通过不代替真实公司账号验收。

## 开发验证的可复现步骤

- Python：`uv run pytest tests/dify/test_self_service.py tests/dify/test_dify_login_workspace.py tests/dify/test_dify_idempotent_import.py`。
  导入检查在固定Dify镜像中运行真实控制器与权限装饰器，数据库是替身，不证明真实数据导入。
- Gateway：按仓内 `verify-aiops-gateway` 技能启动临时数据根；不可指向现役数据目录。
- 前端：固定1.17.1源码，应用全部web-patches并复制同目录组件/service；将
  `aiops-management.browser.spec.tsx`复制到`web/app/components/`，执行
  `pnpm --dir web exec vp test --project browser --run app/components/aiops-management.browser.spec.tsx`。
  浏览器测试使用真实Chromium、合成管理API，不能代替真实公司登录验收。
- 静态检查用`pnpm exec vp check`，生产构建用`pnpm --dir web exec next build --webpack`。
  隔离源码必须有自己的Git根（正常clone已有）；无Git的解包目录会让Tailwind扫描缓存和构建产物，
  出现无效图标错误或内存激增。开发验证先`git init`建立隔离边界；镜像构建沿用版本化脚本。
- 部署桥用`ops/dify-console-bridge/build-bundle.sh`，同时升级登录代理制品以应用成员加入错误提示。

租户内原生副本会先验证CPMS工具身份，再登记为新草稿。显示“尚未发布”时仅预览它自身，
不覆盖当前正式应用。并发发布或来源正式版本变化会要求刷新后确认；重试开通沿用冻结来源。
模型凭据先创建、切换至生效项，再回读能力与预算，不能把“已写入”当作“已可用”。
