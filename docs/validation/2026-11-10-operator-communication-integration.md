# 订单窗口事件候选与通讯源缺口：接线验证

工单 #1454，基线 `b0714ef5`，分支 `feat/operator-communication-evidence`，2026-11-10。
本轮获准应用共享文件集成补丁；旧[首片回执](2026-11-10-operator-communication-evidence.md)
中“未接线、Java未验”仅表示首片时点，本记录为本轮结果。未完成业务验收。

## 新事实与实现范围

父会话报告直接只读 `SHOW STABLES` 无任何comm表；`iot_device_log` 存在但
`SELECT LIMIT 1` 无记录。目标父设备event/fi及枪event均0条，枪property有5条。
这些是父会话实测，本分支没有访问生产；当前不能复原原始报文历史，property只是遥测。

`charging-gun_fi` 经父会话证实存在，本轮纳入第五源：枪调用不由父设备fi代替，
使用已交付订单的 `childDeviceCode`，只取 `_ts/device/functionId`，不读取调用正文。
每源降为12条、总计最多60条，保持24小时上限；所有关联仍为 `device_window_candidate`。
方向、code、decoded、hex、proto、output和其他交易正文继续不交付；界面来源标题为
“订单窗口事件候选与通讯源状态”，不声称已取得本单原始报文。

`operator_cpms.py` 已应用原精确补丁：详情交付前置、当前授权、缓存回读核对、object证据封装。
四项临时入口检查移入 `tests/operations/test_communication_integration.py`：保护源500不得写0条、
模型不得指定设备、详情必须已交付、缓存回读迁站仍拒绝；原枪遥测用例没有这些新工具入口判据。

## 实际检查

- 完整65项本地Gateway/HTTP检查：首次64通过、1失败，348.11秒。既有
  `test_evidence_compare_is_provenance_only[derived-shape]` 走过期结果路径，快照保留了节点派生值；
  单独复现1通过（1.94秒），构建结束后同组复跑65通过（60.42秒）。未改既有用例或延长时限；
  本轮未确定首次过期的根因，不能将一次复跑当成已修复该时序风险。
  结果：`dist/verification/communications-integrated-final.txt`。
- 维护目录通讯22项、入口4项、工具声明与目录/README检查合计74通过（7.25秒）。
  最后新增枪fi显式列和60条上限断言后，22项再次通过；分支覆盖94%，报告
  `dist/verification/communications-integrated-coverage.json`。
- 原五个受控变异在五源候选上均被拒绝：设备范围、24小时上限、真实计数、源错误、读后撤权。
- 全仓ruff静态与格式检查通过；wheel/sdist构建通过；真实隔离Gateway启动、匿名401、
  OpenAPI与退役404通过，证据 `dist/verification/communications-integrated-gateway/aiops-gateway.json`。

## Java构建与实际执行

按[仓内Docker方法](../agents/operator-cpms/communications.md#可重复的本地检查)执行，
项目源码/工具声明/Maven设置只读，输出目录和依赖缓存可写，使用claude UID。
Maven `-DskipTests=false package` 成功；`No tests to run` 表示没有JUnit，未将其记为测试通过。
Jar SHA-256：`2811531eac88a2911451ebb6b8497788e8d14772c9a014572d204001c0a77f14`，
本轮无需修改Java源码；源码构建成功与声明加载成功分别验证。

实际节点能力脚本启动本轮Jar：报告 `request-target/2`、四种schema，工具声明摘要匹配。
以本轮Jar的类和依赖在无外网容器执行既有 `ToolSchemaParity`：11个实际装配工具schema
与Python逐字节摘要一致。另用实际 `CpmsReadTool.call` 驱动容器内回环HTTP合成源，
五源嵌套投影、每个事件源12行、通讯不可用total=null、未知decoded丢弃、交付回执全部通过。
临时Java验证源码和运行器在 `.scratch/comm-java/`；日志在
`dist/verification/communications-java/`。这些是作者检查，不是独立验收或真实公司数据。

Maven入口在临时容器提示无法写默认 `/root/.m2` 后继续；未写入该目录，实际依赖路径显式为
`/var/maven/repository`。未修改宿主配置、未展示设置或凭据。自己启动的容器/HTTP已关闭。

## 剩余交付

未完成业务验收。原Dify/H5、真实Java到生产Gateway整链、目标枪fi窗口和实际动态列/时区
仍由父会话核验，原始历史不存在的缺口不能由候选元数据替代。未运行完整仓库pytest或远端CI。
等待RUN主线合并后再同步，保留其耗时/日志接线；然后正常PR、配套节点声明/执行模板/应用发布。
本任务仍仅本地提交，未push、开PR、merge或deploy。他人工作与工作树保持原样。
