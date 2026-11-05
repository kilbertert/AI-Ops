# 拆闸上线 F4a 回放 + F1/F2/F3/F6 回归验收(41)

- 日期：2026-11-05（UTC）
- 环境：production-41
- 网关：`f4cd56bb`（#1388 拆闸 + #1389 语言回读，CD deploy-41 success）
- 账号：ulink（租户 `1942105476598861824`，operator 入口）
- 基线路由：`operator_order_diagnosis` rev27 @ agent_version_no=28，app `137cc417-f068-43c2-9d4a-43ebd254ea01`
- 对照：同日上午 F1–F6 验收（`2026-11-05-tool-refusal-feedback-f1-f6-41.md`）——当时 F4a 的 `tool_refused:` 落不了 `run.reason`，记为机制性限制；本次即该限制的修复验收。

## 结果总览

| 编号 | 判据 | 实测 | 结论 |
|---|---|---|---|
| F4a | 运行中 restore 换版 → 运行失败；`run.reason` 保留 `tool_refused:` 原始码；不因模型噪声空耗 resample | qa_b1a4d799ae9440029ec96e78c46ed20a；`run_bGckxC5Q_xkFLZFWz1i1Z20nL__E_T4-4s2tCIZLlCs` state=failed、**reason=`tool_refused:OPERATOR_PUBLICATION_CHANGED`**；QA failed `OPERATOR_DEPENDENCY_UNAVAILABLE`；qa 名下恰 1 条 run；snapshot `superseded:true`（授权死后节点的迟到上报被 absorb 收进终局快照） | PASS（修复前 reason=`operator_failed`、QA 误报 MODEL_OUTPUT_UNSTABLE、多一次 resample） |
| F1 | 问其他租户真实订单 `2117192292741931009` | qa_6affb941d7a64ce395ea317e112a0ab7；run `run_4VCGSJq08QgKeeoNx8onAiFSGCmgI74tCkIP-KVWEJc` succeeded；答复「暂时无法查询」未带越权字段；`tool_refusals=[CPMS_INVALID_RESPONSE ×3]` | PASS |
| F2 | 问不存在订单 `2116999999999999999` | qa_996264528d114cdfa10d1ee9a00c0873；run `run_tIQ4z_8S7pEOW8bQjA4Npe5FcmWxO6U_PIXDcR20Y5U` succeeded；如实说明查无此单；`tool_refusals=[CPMS_INVALID_RESPONSE]` | PASS |
| F3 | 已选订单会话（`conv_e4994144761740f2905d0d6a4573ccde`）追问不相关订单 `2115330675982561281` | qa_8c2b26e5eb5740c3a66ef722bae2ce51；run `run_3jPklu4Av7ajU-_FEf6MhKf7LJUYJRFyfd6w5GogKiA` succeeded；改取新订单详情答复，不整轮失败；`tool_refusals=[]` | PASS |
| F6 | 本租户订单 `2116050516255272961` 正常诊断 | qa_c69ca4dd682747609a98e18b58bed0ae；run `run_IwWyrdiXurY_jEA0xThPpqrPODkXCYMBsUpP4Ehmdjs` succeeded（rev29）；答复含详情/电价/账务核验 | PASS |

## F4a 时序

1. POST `/v1/assistant/questions`（F6 同题）→ `qa_b1a4d799ae9440029ec96e78c46ed20a`。
2. QA 进入 `running` 后立即 `POST /v1/dify/restore` `{app_id: 137cc417…, version_no: 26}` → 200 `{"action":"synced","version_no":26}`（rev27→rev28；initiator `acceptance-f4a-replay`）。
3. 节点下一次过执行闸被 403 `OPERATOR_PUBLICATION_CHANGED`；#1379 的闸就地 stamp `run.reason`；#1381 让失败上报只过协议闸照常落库（superseded）。
4. QA 终态 `failed / OPERATOR_DEPENDENCY_UNAVAILABLE`；run.reason=`tool_refused:OPERATOR_PUBLICATION_CHANGED`；无 resample（带码终局不按模型噪声重派）。
5. 清理：`POST /v1/dify/restore` `version_no:28` → 200，rev29 active；F6 同题烟测 completed。

## 判据对照（新机制 vs 修复前）

| 点 | 修复前（上午 F4a） | 本次 |
|---|---|---|
| 拒绝码落库 | 失败上报被同一道 `_current` 闸 403，`tool_refused:` 落不了库，reason=`operator_failed` | 上报面只过协议闸，reason=`tool_refused:OPERATOR_PUBLICATION_CHANGED` |
| QA 终态 | 一次 `operator_resample` 后 `OPERATOR_MODEL_OUTPUT_UNSTABLE` | 直接 `OPERATOR_DEPENDENCY_UNAVAILABLE`，恰 1 条 run |
| 迟到交付物 | 被 403 丢弃，快照无取证 | absorb 并进终局快照，`superseded:true` |

## 附记

- 两次 restore 均以 initiator `acceptance-f4a-replay` 记入 `publish_audit_ledger`，可复核。
- 优链充租户路由（rev3）全程未动；consumer 两路未动。
- F5（旧节点回滚）本轮未重跑：节点镜像未变，闸改动在网关侧，上午对照仍有效。
