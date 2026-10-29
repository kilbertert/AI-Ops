# 围栏JSON修复上线与双端真实回归

Gateway `1.0.0+c6c712d5176c`（PR #1161 合并 `c6c712d5`），CD `40867482166` success，
`aiops-gateway-41.service` active，`business_mutations=disabled`。

## 修复前的确定性证据

域目录迁移（`dc9e3eac`）部署后管家原 H5 订单诊断失败，网关固定原因日志给出
`operator_answer_rejected reason=json_parse` + `OPERATOR_INVALID_ANSWER`。
在 41 上用真实供应商（`deepseek-v4-flash`、流式、两条工具结果、`response_format=json_object`）
同一请求三次：**1 次返回 ```json 围栏**、2 次裸 JSON，`finish` 均为 `stop`。
网关只做 `json.loads`，于是一次合规回答被判定失败。见
[本地根因记录](../validation/2026-10-29-z11-operator-fenced-answer.md)。

## 修复后的真实回归

- **管家原 H5 订单诊断：通过。** 正常登录 → 智能检测 → 选单 → 提交 → 回读：
  `202 queued` → 终态 `completed`，报告 938 字符一段正文在**原页面可见**，页面错误 0。
- `agent_runs` 本次 run `succeeded`、`exec_config_state=matched`、`actual_node_id=node-41`。
- 该时段网关**没有任何** `operator_answer_rejected` 或 `operator_completion_refused` 日志。
- C 端原 H5 快捷检测：6 项全部通过（部署后同一版本）。

## 边界

- 修复只剥**一层**围栏且围栏内必须是单个 JSON 对象；散文、多份 JSON、围栏外有文字、
  残缺对象仍拒绝。参数化用例 4 接受 / 5 拒绝。
- 本地全量 3417 passed / 0 failed / 9 skipped。
- 单次真实入口通过不构成长期稳定性结论；历史故障根因准确率、原生 APK/IPA
  仍未验收。供应商若开始返回其他装饰（前后夹说明文字等），仍会按原样拒绝并留固定原因码。
