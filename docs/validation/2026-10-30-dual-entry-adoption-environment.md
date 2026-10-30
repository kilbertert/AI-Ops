# 启动纳管缺端/环境/目标：41 生产数据核对时发现的缺口

日期：2026-10-30。分支 `fix/dual-entry-adoption-env`。基线 `ffa9ed64`（#1226 合并提交）。

## 事实（只读核对 41 生产库）

`#1226` 部署成功（`1.0.0+ffa9ed645e12`，`/health` ok，`business_mutations=disabled`）之后，
只读查询 `/var/lib/aiops-41/gateway/gateway.db` 的受管目录：

```
137cc417 | 1942105476598861824 | operator | operator_order_diagnosis | (env 空) | managed-adoption
349a637c | 1941847786056323072 | operator | operator_order_diagnosis | (env 空) | managed-adoption
394b5399 | 1942105476598861824 | operator | operator_order_diagnosis | (env 空) | managed-adoption
65255300 | 1941847786056323072 | operator | operator_order_diagnosis | (env 空) | 1941847786450153474
a57602c1 | 1941847786056323072 | operator | operator_order_diagnosis | (env 空) | 1454348737375174653
rows= 5
```

这 5 行同时证明了两件事：**启动纳管在生产上真的跑通了**（#1226 修的就是它），以及
**纳管写入的行 `environment` 是空的**。

## 缺口

启动纳管（`self_service_api.register`）只写了应用与 agent 名，没有写 `entry`/`scenario`/
`environment`/`targets`。对管家行无害（管家不按环境查目标，端/场景的默认值正好正确），
但对**客户端**行是致命的：

- 行会落成 `environment=''`、`targets_json='{}'`；
- 而客户端运行目标按 `(租户, 环境)` 查（`consumer_publication.managed_target`）⇒ 查不到；
- 症状是"现役客户端应用在册，但发布时说它不受管"，而目录里明明有这一行。

这个缺口现在还没在 41 上发作，因为**还没有任何受管客户端应用**；第一次纳管现役客户端应用
就会撞上。它是在核对生产数据时看出来的，不是测试看出来的。

## 修复

纳管时把 `entry`/`scenario`/`environment`/`targets` 一起落库（`environment` 缺失时取部署环境，
`targets` 只在客户端行写入，来自 `_adopt_consumer` 从现役文件推出的目标）。

## 新增回归

`tests/dify/test_self_service_adoption.py::test_startup_adoption_keeps_the_entry_environment_and_targets`：
用真实 `SelfServiceStore` + 一个够用的 FastAPI 替身跑 `register`，断言纳管后的行
`entry=consumer`、`environment=env-a`、`targets` 带对客库，并且
`consumer_publication.managed_target` 按 `(租户, 环境)` **查得到**这条目标。

## 验证

```bash
uv run pytest -n 0 tests/dify/test_self_service_adoption.py   # 5 passed
uv run pytest -n 4                                            # 3625 passed, 9 skipped
uv run ruff check src tests                                   # All checks passed
```

`deploy/m2-live-03/images.lock.json` 重建写回
（`source_sha256=976d2743…`，`image_id=sha256:b04048cc…`，`built_at=2026-10-30T12:53:56Z`）。

## 未覆盖

- 真实纳管一条**客户端**应用（41 上目前没有受管客户端应用），因此这个修复只有单元级证据，
  没有真实环境证据。
