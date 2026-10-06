# 只读诊断 run 的生命周期

## Sub-features

- 创建 run（`POST /v1/runs`，202）
- 按 workspace 隔离查询（`GET /v1/runs`、`GET /v1/runs/{run_id}`）
- 事件增量同步（`GET /v1/runs/{run_id}/events` 按 sequence）
- SSE 实时事件流（`GET /v1/runs/{run_id}/events/stream`）

## How to get to it (user POV)

已注册设备（带设备令牌）创建并跟踪一次只读诊断。

## Driving it

```bash
TOKEN=aops_…
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/v1/runs          # 401 未认证
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/v1/runs         # 200 空列表
```

## Gotchas

- **`/v1/runs` 返回 200 只证明鉴权通过、列表可读**，不证明任何诊断发生过。
  空列表是空数据根的正确渲染 —— 和"run 功能已验证"是两件事。
- **创建 run 会真的去执行诊断**，而诊断依赖远端 `/diag/*`、模型 provider 与
  KB。本地数据根上 `POST /v1/runs` 可能返回 202 却永远不产生结果：
  202 是"已受理"，不是"已完成"。断言时不要把它当成功终态。
- run 按 **workspace 隔离**。用工作区级令牌查另一个 workspace 的 run 应当查不到 ——
  这是隔离契约，不是缺陷。
- 事件接口按 **sequence 增量**同步。只拉一次首屏不等于同步正确；要断言 sequence
  单调且不丢。

## 本地不可验证的部分

诊断本体（`/diag/*` 读 MySQL/TDengine/Redis、模型驱动执行、KB 活性）**需要远端服务**。
若本次改动落在这些路径上，请明说无法在本地证明，不要把本地绿灯当作覆盖。
