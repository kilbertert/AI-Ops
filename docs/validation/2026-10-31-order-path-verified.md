# 订单链路验证通过（2026-10-31）

## 证据（库内，不是界面回显）

```
created_at  2026-10-31T18:27:47  state=succeeded  data_kind=status
order_no    2115290621004312577
context     {"status":"live","live_snapshot":{...,"status_role":"finished"}}
            "fact_contract": "slots"          ← 新契约真的被消费
snapshot    {"blocks":[{"kind":"text","text":"这单当前的状态是「充电结束」。\n\n
             订单号：2115290621004312577\n查询于：2026-10-31 18:27:48 UTC\n\n
             这只是我查到的业务状态，不代表故障原因。\n来源：充电状态记录"}]}
```

正文由 `render_segments` 渲染事实段，模型仅决定片段位置。

## 守卫仍然拦截矛盾（原验收核心要求）

| 输入 | 结果 |
|---|---|
| `帮你查到了。\n{{fact:status_line}}` | 接受 |
| `这笔订单尚未完结，仍在供电。\n{{fact:status_line}}` | **拒绝** |
| `这单已经结束了。`（无片段） | 拒绝 |
| `订单已结束。\n{{fact:status_line}}`（片段外复述状态） | 拒绝 |

## 打通这条链路经历了三个缺陷

1. **库是空的**：`188c54f4…` 0 文档 → 换 `3064598c…`（61 文档）。
2. **审计类型说谎**：知识预览被记成 `data_kind=status`，排查方向被带偏（#1269 修）。
3. **旧 SYSTEM 与新协议冲突**：`恰好三个字段` 与 `reply + 事实片段` 并存，模型照前者执行 →
   `required evidence segment missing`。拆出独立协议项 + 改 4 处冲突句（minimal）后通过。

## 当前草稿状态

```
draft_hash    18eced8e53fdac45…
system        2 条（运营 818 + 协议 1607）
env           + answer_fact_contract=slots
published     2026-10-31 12:15:18 / e7501f0a   ← 仍未动
```

## 前端

新镜像 `aiops/dify-web-patched:trial-errors-07a113ec` 已部署到 36，
失败时会显示服务端 `error_message`（旧版一律显示通用文案）。
回滚：`dual-entry-merged-86d63d4a` + `/tmp/env-before-web-trial-errors.bak`。

## 下一步

A/B 发布闭环：当前已发布 A = 旧版（无 slots）；草稿 B = 本次迁移版。
发布 B → 正式新会话验 → 恢复 A，全程保留回滚点。**尚未发布。**
