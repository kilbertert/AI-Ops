# 运行日志复核行渲染修正（ADR-0252 显示层跟进）

## 背景

- 用户在真实页面上指出：复核开始事件渲染成「事实复核：通过 · 耗时 未采集」——
  复核还没跑就先宣布通过；此前验收只做到容器内 bundle 文案核对，没有把页面
  在真实 run 上渲染出来看，是验收缺口。
- 分支：`fix/run-log-review-rendering`（基线 `30ccee24`）。

## 本轮动作

- `aiops-operator-run-logs.tsx` `describe()` 的 `review` 分支：
  - `status=running` → 「事实复核… · 已进行 X 秒」（与 tool 分支同款，
    不再回落「通过 · 耗时 未采集」）。
  - `status=succeeded` 且 detail 非 `allowed` → 「事实复核观察：<原因>」，
    与顶部「复核观察（答复已交付）」徽标语义对齐——verdict 是观察不是拦截。
  - `status=refused`（复核自身没跑成）仍读作失败：「事实复核：<原因>」。
- `browser.spec.tsx` 新增 3 个用例：running 复核行不误报通过、非 allow
  verdict 标观察、refused 保持失败措辞；参数化覆盖两枚观察原因码。

## 边界

- 纯显示层修正：投影契约不变，后端不动；trace 事件 start+end 各一行是
  既有设计（工具调用同样两行），改的是第一行文案。
- 「业务结果已过期」是保留策略，不在本轮处理。
