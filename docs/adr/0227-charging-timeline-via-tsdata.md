# 227. 时序过程遥测经 tsdata 进工具面：范围由查询构造保证

日期：2026-11-05。

Status: Accepted（PRD #1393 第一项 #1394）。

`cpms_charging_samples` 只读 MySQL `iot_power_runtime`——该表只收
`launch_type='operator'` 的启动单，远程启动订单的过程数据**结构上**不存在
（41 实测 237/237 轮 `no_samples`，是真话不是缺数）。SOP §2.2 的权威过程
源是 TDengine `charging-gun_property` 原始遥测，工具面一直没有这条腿，
于是「拿不到日志」类问题在工具面上无解。

决定：新增 `cpms_charging_timeline`，经公司 tsdata 服务的
`POST /tdadmin/data/query`（`QueryRequest` 参数化，内部拼 SQL，无注入面）
按授权订单窗口取 `charging-gun_property` 行。要点：

- **单独成工具，不改 `cpms_charging_samples`**：监测表视图与原始流是两个
  出处、两个口径，合并会让模型把互相矛盾的样本数当成一回事。
- **响应形状用 `object` 不用 `page`**：节点 `samples()` 投影把 `size==50`
  与 `ordering_guaranteed`/`complete_timeline` 静态标记写死在 page 形状里；
  本工具的完整性是**逐次可算**的事实（`total <= samples_returned`），静态
  标记不管写 true 还是 false 都有一半时间在撒谎。object 形状下 `value`
  由网关逐次合成、节点逐字投影，证据里没有一个字段需要迁就常量。
- **范围由查询构造保证，不经 `validate_body`**：时序行上没有
  `orderNo`/`siteId`/`tenantId` 可逐行核验；`deviceId` 只可能取自已交付
  订单详情证据的 `childDeviceCode`/`deviceCode`，窗口只可能是
  `createdTime`~`stopTime`（`stopTime` 缺 = 仍在充电，上界取网关本地现在）。
  tsdata 端点本身无鉴权——构造来源就是范围闸，详情未交付走
  `OPERATOR_DETAIL_REQUIRED`。
- `_ts` 在证据里改名 `ts`：`evidence_shape` 键名正则不允许下划线开头，
  只改名不改值。
- `fields` 用 `GUN_COLUMN_NAMES`：列清单的唯一权威在 `data/sources.py`，
  诊断直查与只读代理白名单都从它派生，不在第三处复制。
- 上游一切失败形状（不可达/5xx/信封残缺/行形状非法）归一成
  `TSDATA_UNAVAILABLE` → `OPERATOR_DEPENDENCY_UNAVAILABLE`，不落成空页。

部署：`AIOPS_GATEWAY_OPERATOR_TSDATA_URL` 注入，空值按依赖不可用拒绝；
节点侧不用改 Java——它从同一份 `operator_tools.json` 装配（`-Daiops.node.tools-file`
随节点配置下发），`object` 形状与 `request-target/2` 契约都是既有形状。
页大小 64 = 节点 `project()` 的数组上限，`truncated`/`total` 如实标注装不下的部分。
不翻页：第二页不是授权边界的一部分，而是「先证明第一页不够再说」的问题。

## 备选方案

- **复用 `TDengineSource` 直连 `/rest/sql`**：它属于诊断管线的 Settings 体系
  且需要 TDengine 账号进网关环境；tsdata 是公司已有的查询门面，不带新凭据，
  否决。
- **`page` 形状 + 静态 `complete_timeline`**：如上，任何静态值都会有一半
  时间在撒谎；把 flag 改成派生要动节点 `samples()` 分派——那是「新响应形状
  才改引擎」的边界，为一个工具动引擎不值，否决。
- **`txSerialNo` 过滤**：SOP 的充分谓词是设备+窗口，而 `txSerialNo` 不在
  详情证据字段里；为它扩大详情字段不值得——窗口即订单窗口，同枪下一单的
  行天然在窗外，否决。
