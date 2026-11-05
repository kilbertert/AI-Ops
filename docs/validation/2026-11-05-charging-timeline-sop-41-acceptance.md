# 时序遥测第二数据腿与 SOP 蒸馏 — 41 发布与回放验收（#1396）

- 日期：2026-11-05（UTC）
- 环境：production-41
- 交付：#1394 `cpms_charging_timeline`（#1399/`4e461620`）+ #1395 `operator_sop.md` 全量蒸馏（#1400/`fa50eadd`）+ 门面列修复（#1402/`46914c50`）
- 账号：ulink（租户 `1942105476598861824`，operator 入口）
- 节点：`097c8ae9037a-2811531eac88`（工具清单 sha256 `d52e092b`，九工具）
- 判据：`docs/validation/2026-11-05-tool-refusal-feedback-f1-f6-41.md`（回归面）

## 结果总览

| 编号 | 操作 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| 正例 | 远程启动单 `2118228481955586049` 问充电过程/曲线 | `cpms_charging_timeline` 返回 tsdata 遥测，答复引用过程数据 | qa_3f075e7e3e6e48b48f8df8f80950ad35；run `run_4xCx8VdJVXHnodJO4gsRJ_M4kY4mi75vt2EpSx7myl4` succeeded；timeline 证据 13 条 txSerialNo=509、source=`tsdata.gun_property`、窗口 14:26:41~14:28:12 与订单完全一致、complete_timeline=true；答复引用 600V/120A/72kW 恒功率平台与「监测表 0 行为结构性」的解释 | PASS |
| 负例（=F1 判据） | 问其他租户真实订单 `2117192292741931009` | 拒绝码落库可见、不带出越权事实 | qa_94e0f7b70d67435498646130eb58b424；run `run_VhMHGP6FB9g128tYA09JO6FHqtIN3X9jlo6II8uVhec` succeeded；`tool_refusals=[{code:CPMS_INVALID_RESPONSE, tool:cpms_order_detail}]`；答复如实告知无法查询、无任何越权字段；review=allow | PASS |
| F2 | 问不存在订单号 `2116999999999999999` | 说明查无此单、快照记录拒绝码 | qa_10f251629bd443fd914f09496c5807eb；run `run_Qhulj2_-4kwAJqUT1tkEi8YzeB6A8NhBUbquddVmWb0` succeeded；`tool_refusals=[{code:CPMS_INVALID_RESPONSE, tool:cpms_order_detail}]`；review=allow | PASS |
| F3 | 已选订单会话（turn1 选定 `2116050516255272961`）追问 `2115330675982561281` | 澄清每轮一单或改取新目标，不整轮失败 | 会话 `conv_5c5920ae5565405c9e785cf064486325` turn_no=2：qa 完成、run `run_hYhnyEmRFtG70vpCcy2Ua1MZpsoLE1KQ1jg6uNhoNHM` succeeded；模型改取新订单详情并给出完整诊断（WeakSignal 通讯故障分类、全额退款核验）；`tool_refusals=[]`；review=allow；turn_no=3 追问结论继续成立 | PASS |
| F6 | 问本租户订单 `2116050516255272961` | 正常诊断；拒绝清单为空时复核行为不变 | run `run_Rwxe46TuZAEXLzZ45SpJVwrOeyhISpJGI_W4E-kU1Bk` succeeded；七条证据（详情/计费模板/账务/金额核验/监测样本/**时序遥测 64 条**/过程统计）齐全；`tool_refusals=[]`；review=allow。QA 结果随后按保留期回收为 410，属既有行为不影响 run 判据 | PASS |
| F4a | 运行中变更发布版本 | 运行失败、不静默成功 | 本工具面与版本闸无交集；引用 `docs/validation/2026-11-05-tool-refusal-feedback-f1-f6-41.md` F4 详录（`OPERATOR_PUBLICATION_CHANGED` 拒绝已在同构建实测） | 引用既有证据 |

## 发布路径上修复的三个部署面缺口

按序暴露，各自根因独立：

1. **`/v1/dify/publish` 409 `DIFY_PUBLISH_REJECTED: ValueError`** — 复现 `node_publication.candidate()` 定位到 `CPMS tool missing from node template`：网关侧 `/etc/aiops-node-runtime/gateway/operator-exec-manifest.json` 仍是 8 工具清单（新增工具需同步进执行清单模板）。已按声明补齐第 9 个条目（`input_schema` 由 `operator_tools.tool().input_schema()` 逐字生成），属主/权限 0600/aiops41，重发布成功 `synced` version_no=29。备份：`/var/lib/aiops-41/backups-1394/operator-exec-manifest.json.pre-timeline`。
2. **节点回调被 nginx 白名单 404 → `tool_refused:unknown`** — `gateway-callback.conf` 的工具名与 GET 路径白名单未含 `cpms_charging_timeline`/`tsdata/chargingGunProperty`，节点的 authorization POST 命中 `location / { return 404; }`，非 JSON 体使节点归因为 `cpms_unavailable`→`unknown`（与此前站点工具同款故障形态，`render_node_callback.py` 文档原样记录过）。已用 `tools/content/render_node_callback.py` 按声明重渲染（回注宿主 `lua_package_*` 两行）、`nginx -t` 通过后重启 `aiops-node-callback`。备份：同目录 `gateway-callback.conf.pre-timeline`。
3. **nginx 缓冲目录属主陈旧 → 大证据体 500 → `tool_refused:unknown`** — `/var/lib/aiops-node-runtime/{client,proxy}-temp` 属主为旧 `www` 账号（700），当前 unit 以 `aiops41` 运行；小体证据走内存缓冲可过，64 行时序证据体超出 `client_body_buffer_size` 落盘即 `open() failed (13: Permission denied)` → 非信封响应 → `unknown`。已 `chown -R aiops41:aiops41` 两个目录。

## 代码缺陷与修复（#1402/`46914c50`）

- 现象：白名单放行后，时序调用仍整轮失败，reason=`tool_refused:TSDATA_UNAVAILABLE`。
- 根因：tsdata 门面 `charging-gun_property` **没有 `batteryMinTemperature` 列**（充电生产 TDengine 同名表有），整份 `GUN_COLUMN_NAMES` 原样进 `QueryRequest.fields` 被门面按列名拒（`TDengine error: Invalid column name` → 归一成 TSDATA_UNAVAILABLE）。
- 修复：`TIMELINE_FIELDS` 剔除该列；行投影本就容忍缺键（网关 picked 跳过、节点 `project()` 跳过），`evidence_shape` 声明不变——上游补回该列后证据自动恢复完整。
- 实测：剔除后同窗口查询 `code:0, count:13`；正例回放全绿（上表）。

## 原始遥测要点（无凭据）

订单 `2118228481955586049`（远程启动、预付费、设备 `qushi0901-1`、站点 `2094981220835291137`）：

- `charging-gun_property` 窗口 14:26:41~14:28:12 共 13 行、`txSerialNo` 恒为 509（未混入相邻交易）
- `status=3`（充电中）全程，`isReturn/isInsert` 正常；电压建立后稳定 `outputVoltage=600`、`outputCurrent=120`、`power≈72000`（72kW 恒功率平台）
- `errorCode/errorReason=NoError` 全程；停机由「达到套餐金额」触发，曲线截断属正常终止而非故障
- 对照：`cpms_charging_samples`（监测表）对本单 0 行——结构性只覆盖运营商启动单，正是引入第二数据腿的原因
- 另一单 `2115330675982561281` 窗口内仅 1 行：`status=故障`、未插枪、电流功率 0、`errorReason=WeakSignal`——通讯故障分类的直接证据

## 回滚与残留

- 回滚资产：`/var/lib/aiops-41/backups-1394/`（发布前 model-config、provider 快照、exec manifest、callback conf、`gateway.env` tsdata 键前值）；`/v1/dify/restore` 可回 v28 版本。
- 第二 operator 租户 `1941847786056323072`（app `b8690d6d`）按 #1396 范围**不更新**，保持既有版本。
- QA 结果回收为 410 属既有保留行为；run 层判据不受影响。
- `batteryMinTemperature` 缺席期间证据行不带该键；门面补列后无需改代码即可恢复。
