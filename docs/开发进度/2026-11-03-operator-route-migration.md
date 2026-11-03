# 管家端路由迁移到现存应用

日期：2026-11-03　分支：`fix/operator-route-migration`　PR：#1318　基线：`7116edd0`（origin/main）

## 里程碑目标

把管家端 `operator_order_diagnosis` 路由从已删除的 Dify app `2b0629cf` 迁到现存的
`137cc417`，恢复"编辑 → 发布 → 恢复"的生命周期入口。

## 已完成

- 迁移前逐项核对（控制台可达性、模型白名单、工具、清单、`require_compatible`、
  目标解析、已有发布版本）
- 复用 `node_publication.publish` + `advance`（与 UI 发布同一实现）完成迁移
- 运行时 `select_binding` 可加载新绑定
- 真机端到端验证：`qa_e6bf8ddd…` completed，`route_revision=17`

详见[验证记录](../validation/2026-11-03-operator-route-migration.md)——**该文件在
2026-11-03 被订正过四处**，阅读时以它为准。

## 当前状态（2026-11-03 只读回读，非迁移时记录）

```
operator_order_diagnosis（tenant …861824）
  app_id=137cc417  engine=node  agent=agt_727264…  ver=24  rev=17  active
  updated_at=2026-11-03T02:50:51Z
```

## 已知缺口

1. **Dify 侧编辑/发布闭环未验证**：只把路由接到现存 app；运营改 app 并发布、
   发布后新会话生效这一整段未做。
2. **审计账本缺一条**：脚本路径跳过了 `SelfService.publish()` 里的
   `record_publish_audit`，`publish_audit_ledger` 无 v24 记录。
   **不补写、不伪造历史审计、不为补账改生产**；事后说明只留在文档里。
3. **生效的系统协议被同时换掉**：迁移前 rev16 服务的是 `agt_5c3e2e… v1`（系统消息 890 字符，
   不含仓库当前 `OPERATOR_OUTPUT`），迁移后 v24 是 962 字符、含仓库当前协议。
   工具/模型/业务提示词相同，但协议后缀不是同一份 —— 这是行为变更，不是"只换 app_id"。
4. 另一租户（`1941847786056323072`）的 operator 路由未动。

## 下一步

- 业务侧确认第 3 条（协议换版）是否可接受；若要严格只换 `app_id`，需另行安排。
- 补齐 Dify 侧编辑 → 发布 → 新会话生效这条生命周期；**必须走受管入口**
  （页面「发布到管家」或 `POST /v1/dify/management/publish`），这样审计账本才有记录。
- 恢复历史版本走 `POST /v1/dify/management/version`，让 revision 单调递增；
  **不要**把旧 revision 写回（会重新开放回调权限）。
- 两端提示词的两种风格、草稿预览不污染正式、页面发布 → 新会话、恢复 → 新会话、
  历史版本冻结。

**整体仍判"业务验收未完成"。**
