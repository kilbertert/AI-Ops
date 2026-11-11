# 五源共同预算与CI修复验证

PR #1458，分支 `feat/operator-event-query`，候选基线 `d25395d6`，1.5.0；
2026-11-10。本轮只在新交付树修改，未改旧COMM工作树；未访问生产。

## 已复现故障与修复

同一实际回环HTTP，每源固定延迟3秒：旧聚合15.049秒，超过Java工具10秒HTTP时限；
并发候选3.015秒。实际Java `CpmsReadTool.call` 经回环聚合器→五个HTTP源对照：
旧实现10.030秒拒绝、无交付回执，新实现3.155秒交付五源、1次回执。
Java通用时限未改。源各延迟6秒时，聚合4.000秒返回五个unavailable，total均null，不变0条。

实现固定进程级5工作线程与5在途槽位，源共用单调时钟4秒截止点；每次调用不建新池、
不等待槽位、不排队。到截止未完成返回source_timeout，繁忙返回source_busy；输出仍按原五源顺序。
read callable显式接受timeout关键字并取得剩余预算，原fixture同步此真实合同。
取源前后各核验当前权限和订单；线程只读已裁定的设备/窗口，迟到结果不更新已返回字段。

新增一项最小检查用Barrier证明五源同时进入，共同截止返回、在途槽位满时不启动第六个请求，
并确认迟到空成功不能覆盖超时缺口。判据来自已复现的节点时限冲突；原立即返回fixture无法覆盖。
时间与标识符修复：内部存储时间统一parse_stored_time，设备编码集中到valid_device_code，
台账仅登记deviceCode/childDeviceCode与event/functionId两类字段，不放宽扫描或订单语法。

## CI15项失败闭环

原CI 42819940224在旧提交15失败。相同点名集合本地复现15失败/25通过；
其中12项共同前因是自助开通fixture漏掉新通讯工具，复制草稿与源版本工具集合不一致，
在预期的SETUP_DRAFT_CHANGED边界终止。旧manifest/profile场景未完成开通，因而未走到后续拒绝。
改为从同一fixture实际发布的源候选取得工具集合，未改生产自助开通代码。
原篡改草稿仍拒绝，旧manifest及动态profile拒绝恢复；同组40通过（19.93秒）。

相关189项通过；时间/标识符/通讯专项129通过、1原有跳过；新并发用例与通讯入口27通过。
全仓ruff、格式检查和wheel/sdist通过；最终模块23项通过，分支合并覆盖91%（132语句/44分支）；报告coverage-final.json。
实际Gateway技能在一次性根启动，health/匿名401/OpenAPI/退役404通过，服务已清理。
最终全量pytest：4786通过、9跳过，351.31秒（Python3.13.13）；full-tests.xml/txt保留明细。
远端新候选CI待推送后执行，不冒充通过。

## 证据与构建

忽略目录 `dist/verification/source-budget/` 保存 before.txt、after.txt、timeout.txt、
java-before.txt、java-after.txt、maven-build.log、ci-repro.xml、ci-fixed.xml、tests.xml、
grammar-regression.txt、coverage.json、gateway/。本轮复现脚本位于 `.scratch/`：
reproduce_source_budget.py、reproduce_source_timeout.py、java_source_budget.py。
Docker Maven构建沿用仓内只读源码/声明与Maven设置挂载方法；成功，不输出凭据。
Maven没有JUnit套件，因此构建成功不当测试通过；Java证据来自实际工具HTTP调用。
该Java夹具合成授权，不证明生产身份服务耗时或Dify/H5整链。

## 边界

4秒约束取源聚合，不保证公司身份/订单读取与宿主调度始终在Java剩余6秒内；这些环节未被跳过。
urllib socket timeout不等于强杀慢滴流：最多5个尚未退出任务继续占槽，后续显式繁忙，
不创建无界线程/任务，也不让后台结果写回证据。未知字段与跨订单正文投影规则保持。
父会话已确认真实event NCHAR、functionId VARCHAR、_ts TIMESTAMP、UTC ISO返回；仍无comm表。
未完成业务验收。评论闭环、原入口、合并/部署由父会话负责；本轮只提交推送同PR。
