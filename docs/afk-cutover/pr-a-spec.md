# PR-A 任务清单:`feat/hybrid-sources-partial-cutover`

**目标**:在 AI-Ops 仓内将 `HttpSources` 改造为 `HybridSources`,使 4 个查询方法(订单/费用/设备/Redis 流)走 HTTP(`/diag/*`),2 个查询方法(原始报文/过程时序)继续直连 TDengine;通过 fixture 等价测试证明不改变诊断输出。

**约束(本会话共识)**:
- 不改 Java 仓库
- 不改 tsdata 仓库
- 不删生产 `production.env` 任何凭据(那是 PR-D 范围)
- 不动 `docs/diag-query-api-plan.md`(那是 PR-C 范围)
- 不引新依赖(用现有 `httpx` / `pymysql` / `redis-py` / TDengine REST 客户端)
- token 用完即弃,绝不写 dotfiles / repo

**Blocked by**:None(可立即开工)

---

## Acceptance criteria

- [ ] `src/aiops_diagnostics/sources.py` 新增/改造:`HybridSources` 类,实现现有 `DiagnosticSources` Protocol
- [ ] 4 个 HTTP 方法实打实调到 `/diag/order` `/diag/device` `/diag/redis-stream`,携带 `X-Internal-Token` + `X-Request-Timestamp`(HMAC-SHA256,300s 窗口)
- [ ] 2 个 TDengine 方法(`get_comm_messages` `get_gun_samples`)内部走 `TDengineSource`(直连,沿现状 `LiveSources` 的实现)
- [ ] `HttpSources` 的 6 个方法签名保持不变(Protocol 一致);`HybridSources` 替换 `HttpSources` 作为新默认
- [ ] `tests/test_sources.py` + 新增 `tests/test_hybrid_sources.py`:
  - 三份 fixture(ykc_amount_mismatch / ocpp_consistent / missing_tx_data)下 `HybridSources` 与 `FixtureSources` 输出**逐字段一致**(mock HTTP 传输层)
  - TDengine 透传路径用现有 `LiveSources` 的同款单测覆盖(参数一致、SQL 字面量与现状一致)
  - 注入防护(`_safe_identifier` / `_safe_literal`)沿用,新增 `_safe_http_param` 等同类白名单
- [ ] 不删 `LiveSources` `MySQLSource` `RedisSource` `TDengineSource` 中任一类(被 `HybridSources` 复用)
- [ ] 不动 `aiops_diagnostics/config.py` 的 MySQL/Redis/TDengine 配置结构(只新增 HTTP base URL + internal-token 相关键)
- [ ] `doctor()` 仍可调用(MySQL 段因为走 HTTP 而无法直连断言,需要分类:`HTTP OK` / `TDengine 直连断言 OK` / 失败分类)
- [ ] `uv run pytest -q` 全过
- [ ] `uv run ruff check .` `uv run ruff format --check .` `uv lock --check` `uv pip check` 全过
- [ ] 增量变更 ≤ 500 行(超出则继续切片,先合小部分)

---

## 模块改动点(不指文件路径,只指模块边界)

- `aiops_diagnostics.sources`:新增 `HybridSources`;`HttpSources` 保留(可作为"全 HTTP 模式"被显式选择,future)
- `aiops_diagnostics.http_auth` (新):封装 HMAC-SHA256 令牌生成与请求头组装(便于单元测试)
- `aiops_diagnostics.config.Settings`:新增 `http.base_url` `http.internal_token.secret` `http.internal_token.expire_seconds` 三个配置项(均带 `None` 缺省 + 启动时校验"若任一 HTTP 方法被调用则这三项必填")
- `tests.test_hybrid_sources`(新):mock HTTP + fixture 比对

---

## Out of scope(本 PR 不做)

- 生产 `production.env` 任何字段改动
- `docs/diag-query-api-plan.md` 任何文本改动
- `docs/afk-workflow.md` 任何文本改动
- `aiops-gateway` 服务侧改动
- 任何新依赖引入
- 性能优化(基准/缓存)

---

## 评审关注点

- 鉴权头生成与现有 `InternalTokenManager` 同款算法(HMAC-SHA256 + `{timestamp}:{expireSeconds}` 摘要);参考 `backend-v2-domestic/cloud-common/cloud-common-auth/.../InternalTokenManager.java`(仅作算法参考,本仓不引)
- 任何新增 HTTP 调用必须带 `_safe_http_param` 字符白名单(防头部注入)
- 错误信息分类:HTTP 401/403/4xx/5xx 各自明确,不暴露 token / secret / URL 内部信息

---

## 与后续 PR 的接缝

- PR-B 改 `doctor()` 时,本 PR 已经在 `HybridSources.doctor()` 返回"HTTP OK"段;PR-B 进一步分类细化
- PR-C 改 `docs/diag-query-api-plan.md` 时,会引用本 PR 的模块边界名
- PR-D 删 `production.env` 凭据时,本 PR 的 `HybridSources` 必须已经在 main 上(否则 PR-D 改完凭据,AI-Ops 起不来)
