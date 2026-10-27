# WF01独立CPMS节点正式发布与原H5验收

日期：2026-10-27（北京时间）；实现PR #1102合并`bdebc893`，标记`v0.5.4`。
最终审查头`5747bf4`的verify/Workflow policy/spec-tree通过；Devin初审与一次复审共三条意见均修复、回复并resolve。
最后一条试用文案修复按已用完的审查预算未再次触发模型审查，确定性CI通过。

## 交付

CD `40447449110`成功，服务active、health=`0.2.0+bdebc893d162`。
`consumer_flow_contract.py`与合并源摘要一致；两份运行配置部署前后未改变。
已合并Nginx制品增加三个精确POST回调，原片段备份后安装、配置检查及reload成功；
三条新路径无凭据均返回JSON401 GRANT_REQUIRED，旧回调保留。

现役WF01在原应用内从V3/revision2发布为**V4/revision3/active**，callback_version=2。
知识、状态、账务分别为真实HTTP节点；`cpms_status_enabled=true`、`cpms_billing_enabled=true`。
模型配置/提示词/图片关闭特性从原草稿保留；没有公司App改动、没有新增插件或真实业务写操作。

## 真实验收

| 验收项 | 结果 |
| --- | --- |
| 发布前真实试用 | 既有试用API、临时Chatflow、真实baoyun/CPMS；completed，含真实workflow_run_id与状态来源 |
| 原H5知识问题 | POST202，1843ms；原GET completed，页面可见知识引用 |
| 原H5口语状态问题 | POST202，1747ms；原GET completed，页面可见cpms.chargingInfo |
| 原H5账务记录 | POST202，2085ms；原GET completed，页面可见cpms.orderDetail及账务限制 |
| 页面检查 | 14/14，三个业务POST无重发，无脚本错误 |
| 执行记录 | 三个QA各自唯一succeeded运行；真实Dify节点记录分别只有knowledge/cpms_status/cpms_billing一个取数节点成功 |
| 独立业务对照 | 状态和账务都按本次登录主体SQL复核可见，再独立读CPMS；除采集时间外快照字段逐项一致 |

开关关闭/草稿不影响正式/发布关闭/恢复重启能力、撤回与权限反例来自隔离栈73/73，
本次没有在线对真实用户关闭能力做实验。此前全量4096 passed/9 skipped；最终头CI4096 passed/10 skipped，
CI跳过范围与本地环境不同，均不算通过。所有模型账单与全域业务准确率未独立验证。

## 过程偏差与恢复点

- 选单预检最初误以为page_size=1必然只返回一条，实际函数多取一条用于判下一页；纠正脚本后使用首条，未改查询规则。
- 保存草稿后严格比对首次报差异：Dify补充环境变量selector；独立逐字段确认仅为`[env,变量名]`，节点/连线/值完全一致，正式指针未动。
- 运行记录核对脚本最初将字符串路径传给AgentStore报错；改用只读SQL取不可变版本，未创建生产App或改库。

原草稿、原已发布DSL、原路由、候选与最终导出均私有保留；恢复正式服务使用原restore接口恢复V3，
原回调片段在项目备份目录保留。恢复不覆盖业务数据库，不删除新QA/运行/审计。
方法见[发布手册](../agents/consumer-wf01/live-release.md)，产品配置见[WF01说明](../agents/consumer-wf01/README.md)。

**未完成业务验收**：这里仅指故障诊断准确率、长期稳定性和原生手机验收；本次三条只读路径已从原H5验证。
不将2秒左右的POST观测值当成延迟上限，不声称短期凭据不会被Dify运行记录保存。
