# 停用候选安装任务：首次页面提交与现场验收

本事件接续P4 [D37](../acceptance/environment-directory-qa-plan.md)与
[ADR0296](../adr/0296-persistent-candidate-installation.md)。范围只是给已安装的自有实例
成套安装停用候选的配置与运行工件；不登记、不运行业务、不激活。
安装通过不等于候选业务已开通。

## 首次执行暴露真实暂存缺陷并已修复

2026-11-22北京时间约00:57，首个安装任务`task_3342c825eaac623f3bee58179e67d88b`
由中央worker领取后在stage阶段失败：暂存包原写入控制installation引用的`/etc`
配置目录旁，线上`ProtectSystem=full`使`/etc`在服务内只读，`ReadWritePaths`
只有`/var/lib/aiops-41`；宿主同uid的`W_OK`不能证明服务内可写。
目标无installation intent/pending，`attempted=false`，仍`1.32.2`；
租约到期正常收敛blocked，历史保留，未搬移或重写其已保存archives。

修复PR #1635（保留安装引用为控制面配置）与#1636（暂存改入
`AIOPS_GATEWAY_DATA_HOME/installation-tasks/<任务编号>`，目录0700、文件0600）
已合并；同一失败用例经回归复现与修复验证，安装相关回归和一次性Gateway启动验证通过。
控制Gateway现场为`1.34.1+e790361f455f`，管理桥`1.34.0`。

## 页面提交到远端安装：LTA-CSMS候选成套落地

2026-11-22北京时间**02:21～02:30**，正常公司登录环境页（验证码由人读取），
选择124、草稿V9/中央登记R1，重算冻结计划
`plan_e21cc7bdfae1f63f43320b816f3d556f`
（scope `local-file-integrity-only`，配置/制品分项passed；
host-identity、capacity、network、business仍pending，state=blocked、executable=false）。
同计划/同租户重复提交候选准备，返回既有`observed`父任务
`task_9246c3bfd196fbc8e7c760949880e480`，无第二个该计划/租户父任务；
六子任务（现场预检、工作区、知识资源、知识连接、管家候选、客服候选）回读均`observed`。

在父任务卡片选择“安装停用候选：2039889358588481536”并确认，页面提交
`{candidate_task, installation:true}`，安装任务
`task_7a63a90a559b3d567260b2c07909ba36`公开queued，经“等待执行端”被专用timer领取；
控制端仅在自有数据根暂存私有包，远端固定task.py先记意图再调原install事务。
页面卡片依次呈现“等待执行端→本地观察通过”，最终回执：

```text
停用候选安装 · 2039889358588481536：本地观察通过
实际版本：1.34.0+c05b3699024f
注册未执行 · 业务未验收 · 正式入口未启用
```

刷新任务按钮后同一卡片与终态可读。控制库核对任务request/result持久：
`installation=installed`、`registration=not_executed`、`activation=not_executed`、
`business=unverified`，状态observed。

## 目标现场与远端对账

- 124实例现场`/opt/aiops-access-124/installation-tasks/task_7a63a90a559b3d567260b2c07909ba36.json`
  记录先存的操作意图与apply结果`outcome=installed`；
  `installed.json`与`/health`均为`1.34.0+c05b3699024f`，`business_mutations=disabled`，
  服务active/running、PID随事务重启一次。
- 期望摘要`desired.artifact_sha256=621f29…`与冻结计划`artifact_sha256`一致；
  计划`source_revision=c05b3699…`即合并修复修订。
- 远端安装器只成套应用自有配置与运行工件；公司服务、业务库与正式路由未触碰。

## 整页重载回读与退出验收

2026-11-22北京时间**02:48～03:05**，重新正常登录环境页，选择同一环境，
后台任务列表经自动加载与手动刷新，安装任务卡片按新会话可读，
终态显示“已过期”（计划窗口900秒届满后的正常终态标记，记录与版本保持）。
原生退出`/console/api/logout`返回200/success；
旧访问令牌重放`/__aiops/publish/check`返回`allowed=false`、
`SESSION_LOGIN_REQUIRED`、不返回公司身份。
浏览器与临时管理访问通道已结束。

## 边界与未验收项

本片只证明：已安装自有实例的停用候选配置/运行工件安装任务，
可经页面提交、持久领取、远端意图加原安装器事务、同任务回读与退出拒绝的完整链。
首次冷安装主链、目标主机身份、正式双向网络、业务预检/注册/激活与P5双环境业务验收仍未完成；
生效目录仍无该环境，租户与正式入口保持停用。
计划窗口届满后重复执行或新计划安装的恢复路径未在本轮现场验证。

证据私有存档：`~/.cache/aiops-candidate-install-task-20261120/live-20261122/`
（页面截图、任务提交与回执、退出核对；不含明文凭据）。
