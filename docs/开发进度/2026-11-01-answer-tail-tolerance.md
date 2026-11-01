# 订单链路答复解析：容忍尾随标点（2026-11-01）

## 目标

让生产订单查询稳定交付**模型写的正文**，而不是在解析环节把一次成功取数判成失败。

## 已完成

| 提交 | 内容 |
|---|---|
| `#1291` `833e15b7` | `reject_detail` 记"为什么不是 JSON"的**形态**（empty / fenced / json_syntax / plain_text:N），不记原文 |
| `#1294` | `load_selection` 容忍"完整对象 + 标点尾巴"；四处读取点统一走它 |

四处读取点：`answer_selection`、`store_reply_source`、`_has_reply`、
`final_answer` 的通知归一化。**只改一两处会让同一个成因换个位置失败** ——
第三、四处正是 Devin 在 #1294 上逐条指出来的。

## 根因（实测）

同一版提示词下，12 条模型答复里 4 条在正确的 JSON 对象后多一个 `"}`：

```
{"kind":"realtime","answer":"","reference_ids":[],"reply":"{{fact:status_line}}\n{{fact:observed_line}}"}"}
```

不是截断（失败条目比成功的最短条目还短），不是提示词（同一份提示词 30 秒前刚成功）。
取证路径与逐条证据见 `docs/validation/2026-11-01-trailing-brace.md`。

## 验证

- 新增回归 5 条，覆盖四类形状 + 一条"三个读取点容忍度必须一致"。
- 变异验证：关掉兜底 / 改回严格解析 / 只回退第四处，各自转红。
- 全量 pytest、ruff、`context_digest.py --check` 见 PR #1294 的 CI。

## 未完成

- **未部署**，生产仍会拒绝这约 1/3 的答复。
- 部署后要连续多次跑同一订单（真实用户会话、真实订单），确认 `reply_source`
  是模型写的那一份，而不是确定性渲染。
- **业务验收未完成**：41 原应用 / 原 H5 的正式验收仍未全部通过。

## 下一步

合并 → CD 部署 → 同一订单连续 5 次真实会话 → 读 `chat_dify_runs` 的
`reason` / `reject_detail` / `reply_source` 三项。
