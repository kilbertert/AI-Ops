# WF01原H5发布与恢复

正式交付以原H5站点、Gateway管理路由、Dify发布版本及审核内容共同为准；某一侧部署成功不能替代页面验收。
验收结果见[本轮记录](../../validation/2026-10-25-consumer-wf01-live-release.md)。照片输入默认不在这次上线范围。

## 公司完整H5构建

公司仓遵循自己的合并请求流程，源码和补丁不复制到本仓。固定合并修订后，使用公司仓新增的
`tools/build-h5.py --tenant-id <目标租户>`：从干净提交复制完整源码、`npm ci`、uni-app CLI编译。
产物在`unpackage/wf01-h5/dist/build/h5`，私有`build-manifest.json`在dist之外，记录源修订和逐文件摘要。
不将客户tenantId写回公共`config/env.js`，不把编译器退出0作为成功唯一依据，须检查完整index/JS产物。

原工程为Vue2，固定CLI5/Webpack5/PostCSS8及匹配uni-app编译器。业务JS兼容CommonJS，依赖保留原模块格式；
`uni-simple-router`必须列入transpileDependencies，否则多平台条件代码会错误进入H5，页面能显示却不执行onLoad。
CSS沿用兼容原单行注释的解析器，无需批量改业务页面。H5构建不等于签名APK/IPA。

## 先备份再部署

1. 经dev-host核验41/36身份；保存gateway.env、发布注册表/路由/版本的只读快照、原H5站点、Nginx片段摘要。
   数据库用SQLite备份接口取得一致副本，恢复时不能覆盖上线后新增QA/run。
2. 新WF01用目标租户自己的advanced-chat应用和每应用Service key，保留旧应用与节点路由。
   审核内容、凭据和发布目标为0600项目身份文件；先校验再安装，业务开关保持关闭。
3. `AIOPS_GATEWAY_DIFY_CHAT_BASE_URL`用HTTPS。API域名安装[精确回调片段](../../../ops/README.md#wf01知识回调部署)，
   保留一次性Authorization；用已撤销验收凭据验证GRANT_REVOKED，不能重放有效grant。
   H5域名安装已合并的签名媒体GET片段，检查Nginx后reload。
4. 原发布API冻结执行图和内容。官方兼容provider使用本应用独立模型凭据；本轮baoyun需
   `compatibility_mode=extended`、`agent_thought_support=supported`及工作流`enable_thinking=false`，
   否则strict模式不发送关闭参数，可能耗尽预算只留下空正文。模型仍为baoyun，不改其他渠道。
5. H5从公司合并版本构建，逐文件摘要核对，经`dev-host cp --artifact-sha256`暂存。
   先复制新静态依赖，保留旧hash文件供已打开页面使用，最后原子替换index；原站点备份独立保留。
6. 检查在飞作业后按变更单重启Gateway；开关启用前检查真实登录的FAQ/快捷入口200、零脚本错误及照片按钮隐藏。
   再启用WF01，执行原页面问答、续问、媒体、本人数据、跨用户拒绝、安全与人工入口。

## 验收与保留

复用已授权的真实正常登录会话，秘密只在受控进程内读取。原H5浏览器发POST并轮询原GET；
不代发Dify回调，不伪造响应。页面检查必须能抓到真实网络请求和完整答案，不能只看Vue模板存在。
从开发机经受控SSH隧道保留原HTTPS域名与证书校验，先等待本地端口就绪；完成后关闭隧道。
订单范围由本人SQL独立核对，再读CPMS状态/详情与同轮快照比较。账务记录不证明银行到账。

Dify运行ID对应模型节点usage可计量已观察调用；凭据验证调用另记，供应商账单未独立核验。
截图、订单、会话、Dify输入与一次性凭据均私有保留；公开记录只含脱敏结论。
发布清单只能先dry-run；新agent和注册表都应unchanged，再同步已合并清单。不要使用`--prune`删除旧节点恢复点。

## 恢复

需要恢复时先关闭WF01开关并校验服务健康；旧本人订单节点仍保留，不重建其运行记录。
恢复前一版内容使用正式恢复API；切回旧应用时恢复原注册绑定和匹配清单，不能只恢复文件假称指针已还原。
前端按备份恢复旧index及同名静态文件；Nginx恢复原片段，原来不存在的回调片段只删除本次新增项，检查后reload。
保留新QA/run、审计及失败原件，不整库覆盖，不删除工作区或插件来代替业务关闭。

## 语义路由上线后复验

先核对合并修订、CD成功、health版本与`consumer_intent.py`/`gateway_api.py`源码摘要，
并对照部署前后两份配置摘要。新分类沿用现用provider，不需改App或发布绑定。

原H5用正常验证码登录取得新会话；已有验证码时只展开验证码表单再提交，不额外发短信。
页面`uni-modal`组件可能常驻但隐藏，验收应检查可见弹窗，不以DOM存在判失败。
保存会话仅放受控0600文件。会话失败先用正式解析器确认原因，不扫描Redis替换身份。

按原页面输入两句：“我上一个订单现在是什么情况”“帮我查看最新的订单的情况”；
记录POST耗时、QA与conversation_id、原GET正文及助手区域可见性。每QA应有唯一成功的
`chat_dify_runs`、真实workflow_run_id及live_snapshot；用同主体SQL和CPMS独立对照订单与状态。
等待最后一轮快照自然超过180秒，旧GET应410；“还是不行”应提示重述；随后明确查最新单
应在原conversation_id成功。不要改线上数据库/时间制造过期。结束关闭本次浏览器与隧道。
