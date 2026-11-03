# 取完证据后被拒的 run 也要重派

日期：2026-11-03。分支：`fix/resample-after-evidence`；基线：`0b48d127`；PR #1326。
订正对象：ADR-0197 的重采样判据（决策本身未变）。

## 一、怎么发现的

#1325 合并部署为 `1.0.17+0b48d12707d3` 后，用同一张单（`2116050516255272961`）
经真实 Dify 试用入口连跑 17 次：**11 成功 / 6 失败**。

失败日志是 `json_parse json_syntax len=769–1303 closed=False depth=1`：

- `depth=1` + `closed=False` = **结构没闭合**，即输出被切断；
- 不是 #1325 修的那种「通篇正文」；
- 失败前一模型轮 `completionTokens=1698–1840`，其中 reasoning 占大头。

## 二、更要紧的：重采样一次都没触发

日志里没有任何 `operator_resample`。查下来是判据写错了方向：

```text
第一版判据：not task.evidence and not task.delivered and not task.arguments
生产最常见的形状：工具轮先跑完 → 证据与参数都已非空 → 模型最后一轮才吐错
→ 判据恒为假 → 永远不重派
```

也就是说：**这条重采样在生产上覆盖的恰恰是它唯一不需要覆盖的场景。**

## 三、为什么重派是安全的（订正依据）

- **证据有缓存**：`grant` 里 `if tool in task.evidence: return task.evidence[tool]["response"]` ——
  重派时工具返回同一份数据，**不会**再请求一次 CPMS，也不会改动已冻结的事实；
- **参数已固定**：`task.arguments` 用 `setdefault` 写入，重派只能沿用同一组关键词，
  模型换关键词会被 `OPERATOR_TOOL_ARGUMENTS_CHANGED` 挡下（这也正是上一轮复审
  担心的场景 —— 结论是它被更下游的判据守着，不需要在这里重复挡）；
- **run 仍是 `issued`/`running`**：被推进过的 run 不会走这条路。

判据收窄为「run 仍是 `issued`/`running`」。

## 四、验证

- 新增 `test_a_rejection_after_evidence_is_still_redispatched`：派发里走**完整**工具链
  （领授权 → 取数 → **上报证据**），再回纯文本并抛 `AgentRunDispatchError`，
  断言重派次数 = `1 + MAX_RESAMPLES`。
  上报证据这一步不能省：`task.delivered` 只在 `record()` 里被填充，少调它就只能覆盖
  「有证据」，覆盖不到「已交付」（2026-11-03 复审指出）。
- **变异验证**：把判据换回旧的三条件后该测试变红 —— 证明它测的是这条约束，
  不是碰巧通过。
- 全量 **3996 passed / 9 skipped**；`verify-aiops-gateway` 一次性数据根八项全过。
- 镜像内容锁真实重建，`context_digest.py --check` 一致。

写测试时踩到四个坑，都记在这里：
1. 提问里不带单号会走 conversation 路由，订单工具被判 `OPERATOR_TOOL_DISABLED`；
2. `terminal()` 超时后测试自己的 `AssertionError` 会穿透到 worker 的兜底 except，
   日志里看起来像"派发失败"，实际要先把假派发里的断言信息打出来才定位得到；
3. 证据的 `data` 必须与网关**自己投影出来的那份**（`project_evidence`）逐字相等，
   直接回原始响应体会被判 `OPERATOR_EVIDENCE_MISMATCH`；
4. 证据的 `inputs` 来自授权回执的 `inputs`，不是 `order_no`。

## 五、边界

- **未部署**：生产仍是 `1.0.17+0b48d12707d3`（含 #1325 的提醒与重采样框架，
  但判据是旧的三条件版本）。
- **截断本身仍未单独处理**：`model_options` 是 `{}`，输出预算交给上游默认值，
  推理内容会挤占它。本 PR 不决定这件事 —— 是显式设 `max_tokens`，
  还是接受截断、只靠重采样兜住，需要单独定。
- 17 次复测里 6 次失败都属截断；本次修复只让它们**有机会**重派一次，
  截断率高时重派仍可能再截断。
