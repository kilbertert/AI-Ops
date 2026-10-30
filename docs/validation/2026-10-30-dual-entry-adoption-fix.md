# 启动纳管冲突：41 部署自检失败与修复

日期：2026-10-30。分支 `fix/dual-entry-adoption`。基线 `8a995897`（#1224 合并提交）。

## 事实

- 2026-10-30 20:08（北京时间）CD run `41053070025` 部署 `8a995897` 到 41，
  `selfcheck=failed`，主机**自动回滚**到 `1.0.0+e30318737089`。
- 回滚后 `aiops-gateway-41.service` active、`/health` 报旧版本；生产没有停留在坏版本上。
- 网关启动日志（`journalctl -u aiops-gateway-41`）明确：

  ```
  self_service_api.py:73 in register   → service.store.register(...)
  self_service_store.py:258 in register → raise SetupConflict("SETUP_APP_CONFLICT")
  SetupConflict: SETUP_APP_CONFLICT
  ```

## 根因

`register_self_service` 在启动时把 `dify_app_registry` 里每个受管应用**再登记一次**
（actor=`managed-adoption`），这是既有的纳管路径，对已在册的应用必须是 no-op。

#1224 给 `register` 加端/场景时，把 `created_by` 一起放进了"是不是同一条登记"的比较元组。
`created_by` 是"谁先登记的"，不是这条登记的身份；纳管用的 actor 与第一次登记的不同，
比较必然不等 ⇒ **每一次启动都抛 `SETUP_APP_CONFLICT`**。

单元测试没抓到它，因为测试里没有"启动时对已在册应用再登记一次"这条路径；真实环境第一次
重启就抓到了。这正是"只跑 fixture 不等于验收"的实例。

## 修复

比较元组改成登记身份 `(app_id, tenant_id, workspace_id, agent_name, profile_id, entry,
scenario)`，`created_by` 只在 INSERT 时写入。`targets_json` 仍按同一条登记更新
（客户端的目标能力会在发布后变），跨租户/跨端的冲突仍然拒绝。

## 新增回归

`tests/dify/test_self_service_adoption.py`（4 项）：

| 用例 | 断言 |
| --- | --- |
| 换 actor 再登记同一应用 | no-op，`created_by` 保持第一个登记者 |
| 同一应用登记到另一个租户 | 仍 `SETUP_APP_CONFLICT` |
| 同一应用登记到另一端 | 仍 `SETUP_APP_CONFLICT` |
| 同一条登记再登记并带新 targets | 只更新 targets，不动 `created_by` |

## 验证

```bash
uv run pytest -n 0 tests/dify/test_self_service_adoption.py   # 4 passed
uv run pytest -n 4                                            # 3624 passed, 9 skipped
uv run ruff check src tests && uv run ruff format --check src tests
```

`deploy/m2-live-03/images.lock.json` 按仓库流程重建并写回
（`source_sha256=83f02781…`，`image_id=sha256:76ed62da…`，`built_at=2026-10-30T12:17:49Z`）。

## 恢复与证据

- 回滚由主机自动完成，**不需要人工介入**；备份目录 `/var/backups/aiops-41/backup-20261030-200848`。
- 本次失败**没有被重试覆盖**：CD run 的失败结论与主机回执都保留。
- 修复后的重新部署与线上回读见 `docs/acceptance/dual-entry/PROGRESS.md`。
