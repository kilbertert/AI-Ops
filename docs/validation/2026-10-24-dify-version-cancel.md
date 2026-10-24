# Dify 固定发布版本与运行中取消验收

## 原缺陷与修复

通用 `/v1/workflows/run` 丢弃请求体中的 workflow_id，发布新版后会执行新版、最后才拒绝版本不符。
改为版本专用路径。blocking 只有结束后才拿到 task_id，运行中无法请求 stop；改为 streaming，
开始事件即持久化并注册取消。缺失/冲突身份、缺完成事件均失败，不用配置值补证据。

标准映射：Dify 1.17.1 Service API 的版本运行路由、workflow_started/workflow_finished 事件及
`/workflows/tasks/{task_id}/stop`；按 SSE 的 data 帧空行分界解析，累计上限 1 MiB、截止时间不续期。
不建立自动重连或启动请求重试。

## 自动化与反向验证

原定向 158 项保留，新增 9 项，合计 **167 passed**。
新增覆盖：已有新版时执行绑定旧版；开始身份缺失；完成身份错配；开始后 EOF 不重试；
运行中取消、ID 迟到与 stop 500。取消测试用事件握手固定阶段，不用空执行记录冒充成功。
变异：改回通用版本 URL → **1 failed**；丢弃开始事件身份 → **3 failed**；还原后通过。

全量首次 **3793 passed / 9 skipped / 1 failed**，唯一失败是内容摘要锁未更新；
更新锁后复跑部署与 Dify 集合 **47 passed**（含摘要锁检查）；未重复整套全量。
ruff、compileall、依赖一致性、ADR、repo-map、policy 检查及 wheel/sdist 构建通过。
Gateway 技能实测启动、健康、未认证拒绝、登记换 token、认证路由通过。
技能脚本第一次只传 --port 未设置子进程端口，第二次绝对输出路径触发 relative_to 报错；
使用同一 AIOPS_GATEWAY_PORT 并在工作树临时目录落证再转存后通过，未修改技能实现。

## 真实隔离验收

真 Dify 1.17.1、官方 provider、真 Gateway；模型/CPMS/身份/订单目录为合成上游。
原链路 **27/27** 通过；扩展验收第一次 **30/32**，运行中 stop 四项均通过，
版本场景实际运行成功但 Service API 回读未取得身份，不能将它记为版本判据通过。
后续改为按真实 run ID 只读查询隔离 Dify 的 workflow_runs，并核模型收到的版本标记。
最终扩展 **32/32 PASS**：新发布 V2 后绑定 V1 实际仍为 V1，切换绑定后实际为 V2；
运行中 ID 已存、Dify stop 标志已出现、立即撤权、迟到结果不复活均通过。
宿主不发布端口，不开防火墙；本次容器已清理，私有数据目录按 --keep 保留。

## 未完成业务验收

没有真实外部模型供应商、真实业务库、真实 UPMS 或真人 App 验收；生产未手工启用。
取消证明 Gateway 请求 Dify 停止，不证明供应商立即停费；Dify 运行记录仍可能保存短期凭据。
