# 订单窗口事件候选与通讯源缺口

工单 #1454，源规格 #1452 的 COMM-01/COMM-02。当前实现订单范围内五个源的最小候选元数据查询；各源不可用与空窗分别报告。
通讯方向、代码和解码正文合同未确认，不查询、不透传。未完成业务验收。

## 已核对的事实

本轮只读既有 `cloud-charging-pile` 和父会话提供的 `tsdata` master `f44ea003` 指定源码，未执行公司 API 脚本：

- `cloud-charging-pile-data/.../po/IotDeviceLog.java` 映射 MySQL `iot_device_log`，
  含 `deviceId/parentId/cmd/hex/proto/normalState/receiveTime`；**没有**
  `direction/code/decoded`。不能将它当成 TDengine `charging-pile_comm` 的列合同。
- `cloud-charging-pile-core/.../domain/service/impl/IotDeviceLogServiceImpl.java`
  按 `deviceId` 与 `receiveTime` 过滤，按 `createdTime` 倒序；不是 tdadmin 门面。
- `ChOrderInfoController.getOrderInfoDetail` 将订单 `chOrderInfoModel` 放入结果；
  `ChargingController` 创建订单时分别取父设备 `deviceCode`、枪 `childDeviceCode`。
- `tsdata` 的 `QueryRequest.fromJson/buildQuerySql/whereConditions/buildCountSql` 确认
  `table/deviceId/start/end/fields/orderBy/order/page/pageSize` 查询契约：`deviceId`
  过滤 `device`，开始/结束分别是 `_ts >=` / `_ts <=`，计数使用同一过滤条件。
- `KafkaVerticle.buildIotData/getMsgTime` 按产品与消息类型拼表名，`device` 来自
  `device-id`，`event` 来自 `event-type`；`_ts` 来自经防碰撞调整的 Kafka 时刻，
  不是设备发生时间。`fi` 调用记录延迟到回复/超时后写入；不代表设备已执行。
- `TSDataUtil.formatFields` 将 `comm` 对象展平，其余嵌套对象序列化为字符串；
  未定义 `direction/code/decoded` 具体合同。`JmsConstants` 只有消息类型、头与主题名称，
  没有方向枚举。未知正文、`hex/proto/output`、交易号均不透传。
- 父会话再次直接只读 `SHOW STABLES`，未发现任何 comm 表；`charging-pile_comm`
  查询500（Table does not exist）。CPMS 的 `iot_device_log` 存在，但 `SELECT LIMIT 1`
  无记录；当前没有可用于复原原始报文历史的数据，不能承诺恢复已缺失的正文。
- 父会话报告：目标订单父设备 event/fi、枪 event 均0条；枪 property 有5条。
  property 是遥测，仍由 `cpms_charging_timeline` 提供，不能冒充通讯或充电事件。
  `charging-gun_fi` 经 `SHOW STABLES` 证实存在；该订单窗口的枪fi内容本分支未实测。
  这些是父会话提供的观察，本分支未访问生产；不能替代后续入口验收。
- 订单详情的已交付字段含 `deviceCode/childDeviceCode/createdTime/stopTime/status`，
  不含 `txSerialNo`。不能把订单号当协议交易流水号，也不能用设备窗口认定交易一致。

## 父会话须确认的源合同

1. `event`、`functionId` 在实际表上的类型/缺失语义、显式列查询结果，以及当前部署与
   `f44ea003` 的对应关系；源码写入路径不能保证每张动态建表已经具有全部字段。
2. `_ts` 返回格式、查询时间的实际时区；本实现沿用已有门面的业务本地时间请求，并用
   已声明 MySQL 时区检查返回时间。未知时区不会回退到主机默认时区。
3. 若未来启用通讯持久化，另行核验方向/代码及协议解码合同；当前不具备原始历史复原条件。
4. 详情与事件/通讯如何关联；目前没有唯一交易关联，所以仅返回设备窗口候选元数据。

核验回执只保留经挑选脱敏的列名、类型和合成示例；不贴原始报文、订单正文或访问材料。

## 五源选择

保留父设备通讯、父设备事件、枪事件、父设备调用，增加已证实存在的 `charging-gun_fi`。
枪调用使用已授权详情中的 `childDeviceCode`，父设备调用不能代表枪级调用；只保留
`functionId` 调用名与入库时刻，不读输入、输出或推断成功。每源从16条降为12条，五源
总量最多60条；源码已证实相同产品/消息类型写入契约，不新增通用SQL或设备选择入口。

## 已实现的边界

模型入口为 `cpms_order_communications(order_no)`，沿用现有订单授权与对象绑定；
不接收设备、URL、SQL、字段、页码或时间参数。先要求当前订单详情已交付，再重新授权订单，
父设备、枪、创建/停止时间或状态变更则拒绝沿用旧详情。

窗口用既有 MySQL 业务时区解释源时间，UTC 持久时间沿用 `+00:00` 格式；未知时区、
歧义时间、无效时间、倒序或未来窗口均拒绝。只有状态 0 且没有停止时间才取当前时间。
最多覆盖创建起 24 小时，超出时保留原结束点并标明窗口截断；固定五源每源12行，合计最多60行，保持在原64行总上限内。没有父设备不从枪编码反推，不回退查询整个站点。

每源返回 `status=records/empty/unavailable`。`unavailable` 的 `total/truncated` 为 null，
原因区分未配置、缺设备、源不可用、响应无效、取源超时和取源繁忙；不回传 HTTP 错误正文。`empty` 只表示查询
成功且当前有界窗口0条。返回行逐个核对设备标签、时间窗口与升序，未知字段一律丢弃。
行只含 `ts/event/association`，`event` 是原事件名或调用名（通讯行可空），
`association=device_window_candidate` 明确不是订单关联。来源不会互相替代。

## 验收与交付接续

COMM-01：一次性数据根，走当前订单详情授权→代理→证据上报，再调用通讯工具。
核对越权/未交付/详情变化/缺设备/坏时间先拒绝，固定查询范围、字段投影、空行、截断、
错误与查询后撤权。替身必须复现已证实上游合同，不能为使测试通过编造源字段。

COMM-02：父会话另派独立验收，经原 Dify/H5 查窗口候选与源缺口，与独立只读同窗结果逐项核对。
没有工程师确认的真实故障，不宣称根因准确率通过。

Java 已按 `operator_tools.json` 装配 object 工具，无需按名称加分支。集成时仍须同时更新
Gateway/节点声明、执行模板、工具授权白名单及应用草稿/发布；仅合并代码不会给已有应用
自动增加能力。读源合同未确认或入口未验收时，不得宣告该工具可用。

### 集成状态与后续同步

本轮已获准将 `.scratch/operator_cpms-communications.patch` 应用到本分支的
`operator_cpms.py`，不再需要父会话手工补接线。实际代码包含：

1. 导入 `communication`，把新名称加进详情已交付前置列表。
2. 缓存回读调用 `communication.require_current`，防止订单迁站/设备变化后沿用旧证据。
3. `_proxy` 中调用 `communication.query(self, task, run, tsdata_query)`，
   按现有 object 工具封装订单/站点/租户及 evidence；放在时序分支之前。

新模块复用既有 `tsdata_query` 的有限响应体、超时和错误映射，不增加上游 API 客户端。
等待RUN主线合并后再同步该分支；解决共享文件改动时必须保留运行耗时记录与上述拒绝边界。
节点声明、执行模板、工具授权白名单和应用发布仍须配套，当前没有推送、发布或部署。

### 可重复的本地检查

`uv run pytest -n 0 tests/operations/test_operator_cpms.py tests/operations/test_communication_integration.py`
执行65项真实本地Gateway/HTTP检查；通讯4项已从临时脚本归入维护目录，无需内存替换模块。
完整任务数据与公司身份均为合成，创建临时数据根，不访问生产。

Java沿用[节点构建方法](../../../deploy/node-runtime/README.md#受管发布前的实际节点兼容性)。
需要源码只读挂载时，单独开放构建输出目录；Maven设置由容器读取，不显示内容：

```bash
umask 027
mkdir -p services/execution-node/target dist/verification/communications-java
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/services/execution-node,target=/workspace,readonly" \
  --mount "type=bind,source=$PWD/services/execution-node/target,target=/workspace/target" \
  --mount "type=bind,source=$PWD/src/aiops_diagnostics/operator/operator_tools.json,target=/tools.json,readonly" \
  --mount "type=bind,source=$HOME/.m2/settings-docker.xml,target=/var/maven/settings-docker.xml,readonly" \
  --mount "type=bind,source=$HOME/.m2/repository,target=/var/maven/repository" \
  -w /workspace maven:3.9-eclipse-temurin-21 \
  mvn -B -ntp -s /var/maven/settings-docker.xml \
  -Dmaven.repo.local=/var/maven/repository -DskipTests=false package \
  > dist/verification/communications-java/maven-build.log 2>&1
uv run python tools/verification/verify_node_capabilities.py \
  dist/verification/communications-java/node-capabilities
```

`No tests to run`仅表示没有JUnit套件，不是行为通过。实际Java装配与工具HTTP投影证据
见本次[集成验证回执](../../validation/2026-11-10-operator-communication-integration.md)。

### 并发与耗时边界

五个固定源共用4秒取源预算，最多5个进程级工作线程/在途请求；满额直接报告`source_busy`，
不排队。到截止点未完成的源为`source_timeout`，总数未知；源返回顺序仍按工具固定声明。
授权与订单范围在取源前后分别核验，耗时或上游错误不会省略这些检查。
HTTP读取显式接收剩余`timeout`，慢滴流等尚未退出的底层任务继续占槽，迟到结果不能改已交付字段。
不改Java工具10秒HTTP时限；公司身份/订单查验与宿主调度仍可能消耗预算外时间。

父会话已通过DESCRIBE确认event为NCHAR、functionId为VARCHAR、_ts为TIMESTAMP；
门面时间为UTC ISO，仍没有comm表。上述列事实不产生新的正文或关联能力。
