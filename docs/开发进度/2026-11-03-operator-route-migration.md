# 管家端路由迁移到现存应用

日期：2026-11-03　分支：`docs/operator-route-migration`

## 里程碑目标

把管家端 `operator_order_diagnosis` 路由从已删除的 Dify app `2b0629cf` 迁到现存的
`137cc417`，恢复"编辑 → 发布 → 恢复"的生命周期入口。

## 已完成

- 迁移前逐项核对（控制台可达性、模型白名单、工具、清单、`require_compatible`、
  目标解析、已有发布版本），确认两条 agent 结构对等，差别只在 `app_id`
- 复用 `node_publication.publish` + `advance`（与 UI 发布同一实现）完成迁移
- 运行时 `select_binding` 可加载新绑定
- 真机端到端验证：`qa_e6bf8ddd…` completed，`route_revision=17`

详见[验证记录](../validation/2026-11-03-operator-route-migration.md)。

## 当前状态

```
operator_order_diagnosis（tenant 1942105476598861824）
  app_id=137cc417  engine=node  agent=agt_727264…  ver=24  rev=17  active
```

## 已知缺口

1. **Dify 侧编辑/发布闭环未验证**：只把路由接到现存 app；运营改 app 并发布、
   发布后新会话生效这一整段未做。
2. **审计账本缺一条**：脚本路径跳过了 `SelfService.publish()` 里的
   `record_publish_audit`，`publish_audit_ledger` 无本次记录。
3. 另一租户（`1941847786056323072`）的 operator 路由未动。

## 下一步

按复核要求，还需完成：两端提示词的两种风格、草稿预览不污染正式、
页面发布 → 新会话、恢复 → 新会话、历史版本冻结。

**整体仍判"业务验收未完成"。**
