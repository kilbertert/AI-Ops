# PR-B 任务清单:`chore/doctor-and-env-example-partial-cutover`

**目标**:`doctor()` 分类断言(HTTP 段 vs TDengine 段);`.env.example` 注释化"TDengine 凭据暂时保留,等 tsdata 真正补洞后回收"。**不**改生产 `production.env`,只改示例模板。

**Blocked by**:PR-A(`HybridSources` 需先存在,本 PR 才有分类的对象)

---

## Acceptance criteria

- [ ] `doctor()` 返回结构分类:
  - `http`:HTTP base URL 可达 + 至少一个端点鉴权通过 → OK
  - `http.http_unreachable` / `http.auth_failed` / `http.config_missing` 错误分类
  - `tdengine`:沿现状 `LiveSources` 的 `doctor()` 行为(账号只读性断言、库存在性)
  - `mysql` / `redis`:由于 PR-A 已经走 HTTP,本 PR 把它们从直连断言改为"未配置 / 已退役"明示
- [ ] 失败信息**不**泄露 secret / token / URL 内部路径
- [ ] `.env.example` 新增 `AIOPS_HTTP_BASE_URL` / `AIOPS_HTTP_INTERNAL_TOKEN_SECRET` / `AIOPS_HTTP_INTERNAL_TOKEN_EXPIRE_SECONDS` 三个变量
- [ ] `.env.example` 对 `AIOPS_TDENGINE_*` 加注释:`# WARNING: TDENGINE 直连凭据保留,等 git.qushiyun.com/iot/tsdata 补完 token 校验后回收(D 方案)`
- [ ] `.env.example` 对 `AIOPS_MYSQL_*` / `AIOPS_REDIS_*` 标注:`# DEPRECATED: 改由 /diag/* HTTP 接口访问,本仓 HybridSources 仍能 fallback,但生产应禁用`
- [ ] `uv run pytest -q` 全过(doctor 测试覆盖新分类)
- [ ] 增量变更 ≤ 150 行(纯模板 + doctor 分类)

---

## Out of scope(本 PR 不做)

- 任何生产 `production.env` 改动
- 任何代码逻辑新增(只改 doctor 分类 + 模板注释)
- `docs/diag-query-api-plan.md` 改动(PR-C 范围)

---

## 评审关注点

- doctor 失败信息分级:不要把 "TDengine still connected" 写成"production ready"——明确"partial cutover,TDengine 段待回收"
- `.env.example` 注释风格保持仓库现有格式(行内 `# KEY=value` 风格)
- 测试断言不要硬编码 secret 字符串,改用占位符

---

## 与 PR-A 的接缝

- PR-A 已经在 `HybridSources.doctor()` 返回分类结构;PR-B 在此基础上细分错误码,**不**重新发明分类
- PR-B 完成后,`uv run aiops doctor` 在生产应返回:`http=ok tdengine=ok mysql=deprecated redis=deprecated`,**全部不阻断**运行
