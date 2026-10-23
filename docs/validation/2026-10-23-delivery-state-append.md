## 2026-10-23 · 冻结归档指针与交付状态页追加约定

> **未完成业务验收。** 这一篇记的是文档路由与写入约定：判据是路径解析、`git merge-tree`
> 的实际结果与测试。它**不**包含工程师确认的真实故障案例，也**不**是用户侧业务验收。

### 一、活文档指向了冻结归档

`docs/validation.md` 自 #1016 起是只读归档（新记录在 `docs/validation/<日期>-<短名>.md`），
但四份活文档仍写「见 `validation.md` 首节」：

| 文件 | 处理 |
|---|---|
| `docs/agents/m3-user-entry-acceptance.md`（2 处） | 改为指向目录 |
| `docs/agents/dify-ops-first-slice/qa-plan.md` | 同上 |
| `docs/agents/dify-ops-first-slice/live-acceptance-plan.md` | 同上 |
| `docs/agents/current-delivery-state.md` | 同上 |

`docs/开发进度.md` 里的同类引用**未改** —— 那是冻结归档里的历史叙述。

判据：`tests/test_readme_architecture.py` 断言 README 相对链接可解析，全部通过。

### 二、交付状态页的真实冲突（本 PR 自身就是样本）

改 `current-delivery-state.md` 的同时，#1027 合并进 main 也改了它，产生真实冲突：

```
CONFLICT (content): Merge conflict in docs/agents/current-delivery-state.md
```

这是「追加到末尾同样会冲突」的实测证据，不是推测。按本 PR 写进页首的规则解决：
两段都保留，规则放页首，main 的新记录追加到 `## 记录` 之后。

**结论要如实说**：这一项改变的是冲突的**处置方式**，不是冲突的发生率。

### 三、评审指出的一条错误指引

初稿写「先读 `## 一分钟结论`（权威现状）」。实测该节最新条目是 **10-19**，而文件开头
有 **10-23** 的记录 —— 指引会把人送到更旧的结论。改为「按日期倒序找最新的一条，
不要预设哪一节权威」，并把实测数字写进页首。

这条值得记下来的原因：它是一个**看起来更有帮助**的说法造成的错误。把「读哪里」写成
「读这一节」比写成「按日期找」更像指引，而它对这份文件的时效分布是错的。
