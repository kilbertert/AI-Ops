# 优链充管家失效路由恢复与架构复盘

日期：2026-11-05（北京时间）。恢复对象是租户 1941847786056323072 的
operator_order_diagnosis 路由。本记录分恢复回执与架构复盘两节；复盘区分
**真正完成、已有偏移、待改**。

## 恢复回执

**失效确认**：旧应用 a57602c1-6a8d-57cd-8ea0-6cd7fd9031bf 已不存在（Dify 404），
路由停在 revision 2；原 H5 对真实订单提问入口返回 403
OPERATOR_PUBLICATION_UPGRADE_REQUIRED。同租户已有登记未发布应用
b8690d6d-d6a3-56b1-bfc4-11cc50ea29ae（operator-agent/1 合同，工具仅
cpms_order_detail 与 cpms_charging_samples，SOP 逐字保留）。

**预览**：同租户授权账号 2 个 trial run（run_eVSI9…、run_Hc4u…）均 succeeded，
绑定订单 2117192292741931009，chat_binding 为 operator-trial:b8690d6d…。

**发布**：同租户原 Dify 会话对同一草稿（draft fingerprint 与发布前快照一致）
正常发布为 version_no 1；chat_route_bindings 该租户 operator 行 revision 2→3，
publish_audit_ledger 第 95 条（2026-11-05T03:20:44Z）。其余 4 条路由逐字段与
发布前快照一致；主租户与消费者入口未动。

**原 H5 复验**：同题诊断与同会话追问两轮均 succeeded 且答复页面可见
（1085/936 字，同一 conversation_id），run 绑定 2117192292741931009、
route_revision=3；答复含订单事实（status=3 已由后台处理、桩离线触发、
金额与倍率），无"本轮没有选定订单"。

**跨租户**：另一租户（1942105476598861824）账号在原 H5 登录后对同一订单提问，
run 失败 reason=operator_node_failed:model_failed、order_no=""，答复为空，
越权事实零泄漏（金额、倍率、桩态等 7 项特征字符串均未出现）；用户可见错误为
OPERATOR_DEPENDENCY_UNAVAILABLE（retryable=true）。隔离未被穿透，但失败语义
错误，见复盘"已有偏移"第 1 条。

**约束遵守**：未增加应用工具；未把旧无效 app 冒充可用回退点（发布前不切路由，
如发布后出现严重失败应走正常撤回新版本而非伪装回滚）；未写回 revision、
未覆盖业务数据库；旧路由与应用快照、发布前后路由/审计/run 证据均已私有留存。

## 架构复盘

对照的问题：实施是否遵循"模型理解问题 → SOP 驱动工具取证 → 自然语言答复，
权限与核对由系统执行"的设计链路；哪些环节偏离了设计、陷入牛角尖。

### 真正完成（链路成立）

- 模型按 SOP 自主调用订单详情/充电监测，订单号由模型提议、系统验证来源与
  权限后绑定（order_candidate 校引用与格式，grant 阶段 _authorized_order
  做真实存在与归属校验，CAS 绑定唯一订单）；答复为自然语言，事实归属由程序
  按证据核对。
- 本轮在第二个租户的真实入口上复验了同一条链路：预览、正式发布、H5 诊断与
  同会话追问全部 succeeded，与主租户 revision 21 的行为一致。链路本身
  不依赖单租户配置，是可迁移的。

### 已有偏移

1. **工具拒绝仍是致命异常，不是业务结果**（本轮实测暴露，最主要偏移）。
   新合同下"查无此单 / 订单不属于本租户 / 参数非法"是模型提议对象后的常规
   业务结局，但节点 CpmsReadTool.call:169-171 把一切非 CpmsRefused 异常
   （含已携带具体拒绝码的 GatewayRefused）压平为 cpms_unavailable；
   CpmsRefused 本身是 RuntimeException，经 Spring AI 直接冒泡 → run 以
   model_failed 终结 → 用户看到"模型或执行依赖不可用，请联系管理员"且
   retryable=true。正确链路应把拒绝作为工具结果回喂模型，由模型如实答复
   "查无此单或无权访问"；当前实现把常规业务结局报成了基础设施故障，还会
   诱导用户对必然再失败的请求重试。这是绑定期时代"拒绝=违规"假设的残留：
   那时订单由系统侧选定，拒绝确实意味异常；改成模型提议对象后，拒绝成了
   正常输入空间的一部分，失败路径没有跟着改。
   注：同租户"单号不存在"走同一代码路径（_authorized_order 在绑定前取数
   失败），代码可推、未单独实测。

2. **cpms_unavailable 掩盖真实拒绝码**。网关 grant 阶段抛的是
   ORDER_INVALID / CPMS_SCOPE_MISMATCH / CPMS_INVALID_RESPONSE 等具体码，
   GatewayRefused(status, code) 本来带着它，节点 catch-all 丢弃后日志只剩
   cpms_unavailable@CpmsReadTool.java:171（实测节点日志中无原始码）。
   跨层归因断链，每次同类事故都要回网关侧反查。

3. **早期已修正的偏移**（记录在案，详见 2026-11-05-operator-agent-loop.md、
   2026-11-05-operator-agent-live.md、2026-11-05-operator-answer-evidence-review.md）：
   - 最终 JSON 答复协议：模型会在 JSON 外夹带说明，协议整体移除，改自然
     答复加数据引用。
   - `{{path}}` 字段引用占位符：模型会编造不存在的路径（如 checks/6），
     占位符协议整段删除。
   - 答复事实复核：单一语义判定 → 分类判定 → 属性抽取 → 受限推理（过慢）
     迭代多轮，最终收敛为"模型只抽取币种/站名实体，程序按证据核对归属"。
     这些都是在牛角尖里多走几轮再退回正确抽象层的典型，成本已付，
     验收记录已留。

### 待改

- 工具拒绝按业务结局分流：查无此单/越权/参数非法应作为工具结果回喂模型
  （或映射为专用业务错误码），真实依赖故障才报 DEPENDENCY_UNAVAILABLE。
  改动在执行节点（CpmsReadTool 捕获面与拒绝回传）与网关（grant 拒绝码到
  用户语义的映射）两侧，超出本次路由恢复范围，建议单独立项。
- 节点拒绝日志应保留 GatewayRefused 原始码（不带上游正文与凭据），否则
  每次同类事故都要双侧对时反查。

## 范围声明

本记录只证明：该租户该入口在所述问法下诊断与追问可用、越权数据未泄漏。
不证明长期稳定性、任意问法、原生移动端或其他租户同构问题已全部排除；
"查无此单"同路径失败为代码可推，未单独实测。
