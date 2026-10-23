## 2026-10-23 · 告警票在恢复时由守望者关闭

> **未完成业务验收。** 这一篇记的是守望者的行为缺陷与其判据：证据来自当下的运行
> 状态与纯函数测试。它**不**包含工程师确认的真实故障案例，也**不**是用户侧业务验收。

### 一、症状与实测

`#792` 开于 2026-10-14，最后评论 10-15。CD 自 10-23 12:17 起连续 7 次 success。

```
$ node .sandcastle/cd-watch.mjs --dry-run     # 修改前
CD 状态正常

$ gh run list --workflow=cd.yml --limit 8 --json conclusion
success ×7（12:17 起）
```

守望者当天跑了 3 次，每次判定正常，每次都不动那张票。

### 二、根因

```js
if (result.ok) {
  console.log("CD 状态正常");
  return;        // 没有关闭路径
}
```

`announce()` 只有开票与评论两种动作。

### 三、改法

`recoveryOutcome()` —— 纯函数，四条分支：

| 输入 | 结果 |
|---|---|
| 取数失败 | `unknown-keep-open` |
| 仍有告警 | `still-alarmed` |
| 健康、无开票 | `nothing-to-close` |
| 健康、有开票 | `close` |

健康时：先评论（「已恢复正常」），再 `gh issue close --reason completed`。

### 四、判据

```
$ node .sandcastle/cd-watch.test.mjs
watchdog 自检通过（32 条）

$ node .sandcastle/cd-watch.mjs --dry-run
## CD 已恢复正常
最近 10 次已完成的 CD 全部 success，且没有卡住的 run。
自动关闭：本票由 cd-watch 在状态恢复正常时关闭。
```

变异验证：把 `unknown-keep-open` / `still-alarmed` / `nothing-to-close` 三处 `if`
各改成 `if (false)`，各自转红一条。

### 五、一个顺手修掉的缺陷

第一版在 `--dry-run` 时跳过票查询，于是预览在一个有开票的仓库上打印「CD 状态正常」。
dry-run 的意义正是预览「会不会关票」，跳过查询等于让它答非所问。改为**也查询、
只读、不执行**。

### 六、评审指出的一处声称错误

关闭评论第一版写「最近 N 次已完成的 CD 全部 success」。**不成立**：`assess()` 的
`ok` 只要求「连续失败 < 3」且「无超时未完成的 run」，`[failure, success, failure, …]`
这样的窗口也算 ok。

改为陈述真实判据（「开头连续未成功 N 次，阈值 3；没有超时未完成的 run」），并显式
写明「**这不是**『最近 N 次全部成功』」。

新增两条措辞测试，钉住关闭评论不得声称「全部 success」、且必须说明真实判据。
变异（把措辞改回「全部 success」）转红。
