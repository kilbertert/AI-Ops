# 平高95：页面真空机初装与候选准备、本机回调入口

2026-11-22 22:44～23:50（北京时间），在用户已批准的「受限初装与候选准备」窗口内完成。
本次事件使用已合并`41b09d5b1622d13cd31af756c76074c804da5d3d`（#1661）的页面与原受控机制，
不经CLI预装冒称页面通过。私有账本与制品留`pinggao/live/`，业务标识不在本文展开。

## 页面冷装与六步候选准备

- 原页面新增95草稿（环境/租户均`enabled:false`），同属判定逐字段一致；
  冻结计划→选择精确已发布客服模板→显式确认「空机初装并准备」，同源重复提交返回同一父任务号。
- 父候选任务终态`observed`，七个子任务全部`observed`：
  site-install（第二论`already_installed`——已安装目标不重装）、site-preflight、
  workspace-prepare、knowledge-prepare、knowledge-connect、operator-prepare、consumer-prepare。
- 父转`observed`早于计划`expires_at`约413秒，即`resources_prepared`在窗口内真实达成。
  页面卡片在计划过期后对已完成父显示「已过期」属展示语义，库内终态保持`observed`。
- 重载后父卡仍按原编号读回；退出登录后旧会话复放返回`allowed:false`/`SESSION_LOGIN_REQUIRED`，
  未回身份。

首轮父任务曾`blocked`：`SETUP_KNOWLEDGE_FAILED`，根因是目标知识服务端为幂等注册——
旧撤回栈11-14写入的凭据与41本轮新生成值漂移。已用其设计接口`POST /kb/tenants/rotate`
按租户头收敛，`matches:true`后重放父任务成功；秘密只经受控通道，不落库外证据。

## 目标本机回调入口（95 extension片段）

新Gateway健康（`site-preflight`观察通过、`/health`200、127.0.0.1:8788）后，
按`ops/environments/gateway/ingress.md`固定步骤挂入`ingress-corrected.tar.gz`
（制品摘要与`REVISION`核对一致；首次安装，原目录仅`site_total.conf`，缺席已记账）。

安装前置核对：宿主有realip模块且全局无`set_real_ip_from`/`real_ip_header`，
`$realip_remote_addr`即真实连接方；各`log_format`不含Authorization；
`site_total`记录cookie故本入口不得复用为浏览器会话代理；
error_log均为默认级别、无`debug_connection`。

验证结果：

| 检查 | 结果 |
|---|---|
| 非获准来源POST/GET各内部路径 | 403（伪造XFF+Bearer不助放行） |
| 明文HTTP同一内部路径 | 403 |
| 未知`/v1/internal/`路径 | 404 |
| 获准来源POST能力回报（匿名/空体） | 网关422 `INVALID_REQUEST`（已抵达业务层） |
| 获准来源错误方法 | 405 |
| 获准来源知识目录匿名 | 403 `KNOWLEDGE_CATALOGUE_FORBIDDEN`（业务层拒绝） |
| 公司既有路径`/charging-pile/`基线 | 200，nginx -t/reload前后一致 |

## 真实节点能力回报链路

95专属共享节点（41上`aiops-environment-95-*`两容器）的`CapabilityReporter`每30秒
经egress:9443→本片段→目标Gateway发起真实POST。从节点容器内重放同一形状与真实凭据，
链路贯通至鉴权层，返回`403 NODE_NOT_AUTHORIZED`：当前尚无运行目标
（`operator_apps`空、无targets文件）。按既有合同，能力回报的前置是
`candidate-register`写入运行目标而非正式激活——停用环境登记后亦可上报
（见124登记回执）；本窗口止于此，未按合同手工伪造。

## 结束状态

- 目标注册表`environment.json`：环境`enabled:false`、租户`enabled:false`，revision 1。
- 正式路由0条；未创建模型问答、grant、发布或准入；公司服务未动。
- 旧会话已退出且拒绝复放；页面草稿与生效目录保持停用。
- 遗留接受项：能力回报INSERT待运行登记（candidate-register）；正式业务矩阵待开通窗口；
  第二真实租户仍缺。
