# 322. 过程统计与开通验收消费同一授权时序证据而非仅监测表

状态：已采纳；平高实测（订单 2123706282732900353，launch_type=admin）暴露的
结构性错配修复。

SOP §2.2 早已写明：`cpms_charging_samples` 监测表视图只覆盖运营商启动单
且保留约 7 天，远程或普通启动单拿到 0 行是**结构性结果**——不证明设备
正常，也不证明时序库没有数据；过程事实以 `cpms_charging_timeline`
（设备+窗口由已交付订单详情裁出）为准。

但两处实现把 SOP 的兜底判成了不合格：

- `cpms_order_process_stats` 的取证前置硬要求 `cpms_charging_samples`
  已交付，统计只在监测行上计算——监测表空的订单（远程/普通启动、汽车桩）
  过程统计这条 SOP 路径根本走不通。
- 开通验收 `operator_evidence` 硬要求监测样本非空且行数等于统计消费数——
  平高租户监测表整租户 0 行（同订单时序 3269 行、单一流水号、字段齐全），
  异常订单腿被判 `PROCESS_EVIDENCE_REQUIRED`，而发布合同要求三能力全集，
  该租户永远无法走完「验证→发布→恢复」链。

## 决定

统计与验收统一消费**经过授权的同单/设备/窗口证据**，二选一：

- `cpms_order_process_stats` 取证前置从「detail + samples 已交付」放宽为
  「detail 已交付 + samples/timeline 至少其一已交付」；行是否真的非空由
  统计再判，两源均无行照旧拒绝，不落零样本统计对象。统计口径：
  监测表有行仍走监测字段（voltage/current/power，兼容不变）；
  否则走时序字段（outputVoltage/outputCurrent/power）。
  行级 `txSerialNo` 多于一个说明窗口混入相邻交易——SOP 分段不合并，
  统计同样不跨流水合并：metrics 留空并在 limitations 说明，不猜哪一段
  属于本单（详情证据里没有可绑定的流水号）。
- 验收门：`cpms_order_detail` + `cpms_order_process_stats` 必备不变，
  过程证据改为「监测表路径**或**时序路径」择一满足。时序路径额外核验
  证据构造绑定：`device` 逐字等于详情 childDeviceCode/deviceCode、
  `window_start` 等于详情 createdTime、已完成订单的 `window_end`
  等于 stopTime（未完成订单按运行中语义不比对上界）、
  `stats.sample_count == len(timeline.records)`、统计至少一项指标非空。
  只凭「时序有行」不通过：设备/窗口/行数任一不符照样拒。

## 被否方案

- 验收门放宽为「时序有行即过」：否决。窗口混入相邻交易、设备/窗口张冠李戴、
  行数与统计消费不一致都拿得到非空时序——不核绑定等于授权一个伪造面。
- 统计跨 txSerialNo 直接合并：否决。SOP 要求分段叙述不合并；详情证据里没有
  可绑定的流水号，跨流水平均出来的功率不归属任何一单。
- 给统计输出加 source/segmentation 字段：否决。节点回报对证据形状逐字段
  比对，加键就是改合同；来源与分段说明走既有 limitations 字段，形状不变。
- 监测表空时判 PROCESS_EVIDENCE_REQUIRED 维持现状、让平高走人工验收：
  否决。发布合同要求三能力全集且监测表空是结构性结果，人工绕行等于把
  SOP 的权威数据源永久判废。
- 改公司监测管道让远程/普通启动单也写监测表：否决。任务边界只允许改本仓，
  且 SOP §2.2 已把时序定为该场景的权威源，不需要上游改行为。

## 后果

- 发布合同不变：三能力全集要求保留，部分发布照旧拒绝。
- 证据形状不变：统计输出仍 sample_count/metrics/average_power_kw/
  limitations/complete_timeline，节点回报逐字段比对不受影响。
- 监测表有行的订单统计口径逐字节不变；验收对监测表路径的判据逐条保留。
- 拒绝方向变严一侧：两源均空现在拒绝取证，与「途径不存在就明说缺口」一致。
