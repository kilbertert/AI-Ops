# 管家答复复核降为观察：实现验收

2026-11-12 UTC。本轮只修改 AI-Ops；用户明确指示本阶段先让诊断成功且顺利完成，
语义复核校验不再拦截交付。工作树分支 fix/operator-review-observe，基线 402e7e4d。
用户已授权本轮提交、PR、评审后合并、配套部署及原入口复测；正在交付。**未完成业务验收**。

## 变更面

`operator_answer_review.review()` 照旧执行并记录，仅交付闸从 `verdict != allow`
改为 `model_failure is not None`：

- reject/uncertain/unavailable 不再拦截答复；verdict 与 reason 仍写入
  snapshot.answer_review、复核 trace、行级日志 review_reason。
- `delivery_mode` 按有无答复给出 `diagnosis`/`evidence_only`，与 verdict 脱钩。
- 复核 trace 的 status 只在复核自身不可用（unavailable）时记 refused，
  reject/uncertain 均记 succeeded（复核成功评估出否定结论不等于复核失败）。
- `_worker` 移除两处 `task.answer_review → OPERATOR_ANSWER_NOT_GROUNDED` 翻译；
  派发成功但 run 未推进统一按 no_answer_accepted 重采样，上限后 UNSTABLE。
- 复核模块本身未改，纯函数语义不变。

保留的闸：`contains_secret`、订单/站点再授权、scope/grant、EVIDENCE_MISSING/
MISMATCH、工具契约、DETAIL/TARIFF_REQUIRED、INVALID_ANSWER 形状、
model_failure 证据兜底、OPERATOR_MODEL_OUTPUT_UNSTABLE 重采样上限。

已知观察噪音：gun 编号（如 `qushi0902-1`）被摘录成站名会留下
`unsupported_station` 原因码——不拦截，恢复拦截前再评估修正。

## 自动化与构建

- 定向测试：`test_operator_answer_review.py` + `test_operator_failure.py` +
  `test_operator_run_logs.py` 共 97 项全部通过，含两个翻转为
  「拒绝/不确定/不可用仍交付且 verdict 落快照」的闸语义测试。
- 全套件 `pytest -q -n 4`：5033 通过 / 9 跳过 / 0 失败（退出码 0）。
- `verify-aiops-gateway` 一次性数据根实跑通过：health 只读、业务写禁用；
  匿名问答/管理 401；OpenAPI 200；`/v1/runs`、`/v1/enroll`、
  `/v1/standard/diagnoses`、`/v1/dify/tools/order_snapshot` 均 404。
- ruff check / format 干净。
- 未触及节点 JAR、Dify 提示词、发布路由与数据库结构；只改 Gateway Python。

## 独立反例与风险

独立 Hacker：claude-deepseek / deepseek-v4.1-flash，会话 aa2cfdb6（148 轮，
探针 /tmp/hack0252/probe*.py，候选工作树实测对照 main 基线）。

oracle 正向面全部实测通过：reject/uncertain/unavailable 全交付且
delivery_mode=diagnosis、verdict 落快照/轨迹/行级；model_failure 时复核一次都不调；
凭据闸（bearer 入文→403 INVALID_ANSWER）、取消/撤权仍挡、重采样耗尽归因与基线逐字相同。

发现与处置：

| 发现 | 处置 |
|---|---|
| 复核后授权/范围闸拒落入 no_answer_accepted 重派+误归 UNSTABLE（实测 dispatches 1→2） | 已修：闸拒并入 stamp 约定+`completion_refusal` 兜底，回归测试锁 attempts=1+原码 |
| 已交付的 reject 答复，日志页仍渲染「诊断未完成原因」 | 已修：delivery_mode=diagnosis 时改标「复核观察（答复已交付）」，浏览器用例新增该组合 |
| `review()` 未兜 AttributeError，复核自身坏掉会拦交付（基线同缺陷，非回归） | 已修：收敛进 unavailable 元组，新增 settings/provider 缺属性用例 |
| verify_operator_cpms.py 仍按旧语义断言（ED4 必失败） | 已修：替身 reject 断言改为 diagnosis 交付+verdict 落快照 |
| llmdoc operator-chat/execution-model 逐字描述旧闸 | 留待合并后 /llmdoc:update 增量同步 |

未核到/留观察：闸拒重派在生产上的真实频率（本轮无生产访问）；额外一轮模型代价未测量；
verify_operator_cpms.py 需真实容器未实跑；AttributeError 在正式配置下是否发生未验证
（AgentSettings 有 default_factory，属防御面）；delivery_mode 其他消费方未穷尽。
另：已交付的 reject 答复会作为 assistant 历史进下一轮上下文——「已交付即已接受」的
自然结果，不计为缺陷。

## 边界

- 本地验证不覆盖真实管家问答端到端：合并部署后按原入口复测一笔真实诊断，
  确认 completed 且 delivery_mode=diagnosis（或证据兜底时 evidence_only）。
- 复核只降交付闸不降记录义务；verdict 仍可对后续分析/恢复拦截提供依据。
- 历史「诊断未完成」记录不改写；本变更不回填旧数据。
