# 86. workspace 名字里带公司名，但对账按**存下来的**那个名字

日期：2026-10-22
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_workspaces.py`（`workspace_name` / `workspace_name` 列 /
`mark_created`）、`src/aiops_diagnostics/dify_ensure_workspace.py`（对账依据）、
`admin_cli.py`（`ensure-workspace --company`）

## 背景

Dify 的 workspace 切换器把名字显示成一行短文本。原来的派生名是
`aiops-{environment}-{tenant_id}`，19 位 id 在其中被截断成
`aiops-41-194210547659886...`（2026-10-22 截图）——**人看不出这是哪家公司**。

但名字不能随便改：`ensure-workspace` 的**对账**按派生名精确匹配，而 Dify **没有删
workspace 的 API**。名字对不上 ⇒ 对账命中 0 ⇒ 判"没建过" ⇒ 重建 ⇒ **永久孤儿**。
ADR-0059 因此写着「缓解是不给运营改名的入口」。

## 决策

**名字在"建的那一刻"取一次，写进 `dify_workspaces.workspace_name`；以后对账一律按那一列。**

```
建：name = workspace_name(env, tenant_id, company=公司名)   → aiops-41-Ulink-598861824
表：UPDATE … SET workspace_id=?, workspace_name=?          ← 同时落盘
对账：reconcile_by_name(name = row.workspace_name)         ← 用存值，不用当前派生值
```

格式：`aiops-{environment}-{公司名 slug}-{租户 id 后 8 位}`。

## 为什么是"存"而不是"重算"

公司会改名。`Ulink` 改成别的之后，**当前派生值**变了，而 Dify 里那个 workspace 还叫旧名。
按当前派生值对账会命中 0，于是重建 —— 而 Dify 删不掉，那就是永久孤儿。

**存值让"这个名字是它出生时的名字"成为事实**，改名不会倒过来影响对账。

## 三条刻意的取舍

**一、公司名只影响建的那一刻。** 之后的改名不重算、不改 Dify 侧 —— 名字是历史，不是状态。

**二、保留 `aiops-` 前缀。** 名字要在 Dify 的 workspace 列表里与别的（未来可能由运营手工
建的）workspace 区分开。

**三、id 只留后 8 位。** 全 id 在切换器里被截断，留全反而更难读；后 8 位加上公司名已经
足够人认，而**机器认的是 `workspace_id`**，不是名字。

## 被否掉的替代形态

- **不加公司名，维持纯 id。** 切换器里显示 `aiops-41-194210547659886...`，看不出是谁 ——
  而"运维在切换器里选哪个租户"正是这次要解决的场景。
- **名字里只加公司名、不放 id。** 公司名不唯一（同一个集团下可能有两个同名主体），
  而 `tenants.name` 在 Dify 里**没有唯一约束**。
- **每次对账重算派生值。** 见上：公司改名即孤儿。
- **把公司名同步进 Dify 并保持"名字 == 当前派生值"这个不变式。** 那要求每次公司改名都跑一次
  迁移，而且迁移失败就回到孤儿状态 —— 比存值脆。

## 后果与代价

- **`dify_workspaces` 多一列**，老库靠 `PRAGMA table_info` 补列（`CREATE TABLE IF NOT EXISTS`
  不会给已有表加列）。
- **改名字要同时改两处**：Dify 侧 `tenants.name` 与表里的 `workspace_name`。只改一处会让
  "表说叫 A、Dify 里叫 B"，而对账按表查 —— 那时它是**对的**，但人看到的名字与表不一致。
- **公司名 slug 会截断到 24 字符**，超长的公司名只保留前半段。
