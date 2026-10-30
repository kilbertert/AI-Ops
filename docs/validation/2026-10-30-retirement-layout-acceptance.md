# 退役与领域目录独立验收

2026-10-30，北京时间09:29起；验收基线`6bcc7fda`，生产Gateway`1.0.0+97fdcf65bc35`。
复核#1153 `8acc9045`、#1159 `dc9e3eac`、#1161 `c6c712d5`、#1165 `b571a256`。
四个PR均MERGED，源码修订的必需CI通过，CD已成功；Devin额度不足占位success不作为完整审查。

## 本次复验

| 范围 | 结果 |
|---|---|
| 12领域模块迁移 | 独立比较业务AST相同；调用与测试同步，无旧转发壳 |
| 生产领域源码 | 12文件摘要与本次主线一致，12旧根文件缺失；Gateway只读且active |
| 全量与格式 | 3429 passed / 9既有条件跳过；ruff/format通过 |
| 构建与启动 | wheel/sdist成功，新包及运维entrypoint完整；内容锁一致，临时Gateway八项通过 |
| C端原H5 | 正常登录、当前QA、完成、语言、非空和正文可见6项通过 |
| 管家原H5 | 正常登录、202后failed，OPERATOR_DIAGNOSIS_FAILED；页面错误0，没有结果可验 |

管家同QA唯一run在工具阶段失败，reason=operator_node_failed、exec_config_state=expected、无快照；
节点窗口日志为CpmsRefused: cpms_unavailable，不是此前JSON围栏拒绝。
生产Java仍为`2da807b4`，jar摘要`ca6672176c6e81d7378dd4e2b0f75f7016459e4a82803ec4b8642a49983e79eb`，
类文件不含electricityFee/samples_returned；后续#1168要求Gateway与节点同版，当前部署不满足。
已从合并`97fdcf65`归档源码离线构建新jar，摘要
`51b2498100954a09561b88a62245201b8cb969466a3f64cd721dbcfc8d0f96cf`；真实Java/Gateway隔离整链通过，尚未部署。

## 规范与范围复核

规范轴2项：#1161/#1165缺开发进度记录，本记录配套新增补记；领域片序号与旧模块数量不一致，统一按PR和12模块描述。
需求轴2项：README八片/独立桥旧包摘要失真，已修正；全部目录任务仍未完成，不能把领域片当整仓完成。
用户已确认继续全部剩余迁移：通用、会话、Gateway与服务入口，以及工具/集成/部署资产归类。
包根目前29个Python文件（含初始化），测试根90个Python文件；这是剩余范围，非新缺陷计数。

## 边界与操作

已交付退役和领域迁移的代码范围复核合格；**当前管家业务验收不通过**，不以历史z16成功覆盖本次失败。
历史故障根因准确率、长期稳定性、原生APK/IPA未完成业务验收。
生产访问按env-41-runbook与runtime-retirement-operations，读取SQLite使用mode=ro，未构造第二个生产app。
本次登录/QA保留正常生命周期，仅清理自己创建的浏览器和隧道；原始账号、订单、日志、截图保存在私有证据目录。

## 同日补部署后的复验

另一交接会话按已合并源码补部署Java `6bcc7fda-51b249810095`，摘要与本会话独立构建一致；本会话没有重复部署。
生产current已为6bcc7fda。09:50后本会话重新正常登录原管家H5、选单、提交，QA completed，报告原页面可见、页面错误0。
精确qa_id关联唯一run succeeded/matched/node-41，快照含cpms_order_detail和cpms_charging_samples。
基线失败与修复后成功分别保留；本次退役/领域批次回归验收通过，不代表长期稳定性或故障准确率通过。
