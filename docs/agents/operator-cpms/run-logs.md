# Dify 管家运行日志

需求与判据来自 #1452 的 `order-feedback` 规格，LOG #1455 依赖 RUN #1453。
读取与授权决定见 [ADR 0242](../../adr/0242-operator-run-metadata-readback.md)。
在 Dify 应用原 `/app/<app_id>/logs` 入口，已登记的受管 operator 显示本人 Java 运行；
consumer 与普通应用沿用原生日志。失败运行也在列表中，不伪造 Dify 会话。

同源 `POST /__aiops/manage` 增加两个固定动作：

- `run-logs`：`app_id`、`page`（1–1000，默认 1）、`limit`（1–50，默认 20）。
- `run-log`：`app_id`、`run_id`；详情与列表使用同一筛选。

桥每次核对 Origin/Referer、CSRF 会话、工作区角色和 Dify 应用访问权，
再转发 `/v1/dify/management/run-logs` 或 `/detail`。Gateway 使用机器门与该会话，
查询当前公司身份、重解调用者并刷新场站范围；从已有受管目录只读应用归属。
按 `publication_app_id`（不是 Snail `app_id`）、本人、租户、权限指纹及
`operator_agent` 操作筛选。权限指纹变化即不显示旧记录，即使只是扩大范围。
跨公司、跨应用、跨人、撤权及权限源失败不能取得详情；分页不接受身份覆盖字段。

响应字段限定为运行/QA 引用、有效状态、UTC 开始时间、耗时及来源、结果是否过期、
阶段与工具名、已核对版本、Gateway 版本和节点声明的构建。无问题全文、订单号、
答案、快照、失败原文、工具参数或原始 data。详情不是业务结果回读，不调用或绕过
`authorize_readback`；不修改账本状态。已到期的 `issued/running` 在响应中显示 `expired`，
成功/失败等终态保持原值，结果时效单独展示。读取使用已有 `agent_run_store`，不新建 store。

时间沿用 RFC 3339 UTC 表示（`format_stored_time`）；阶段耗时来自 RUN 的单调时钟记录，
页面说明“截至最后已采集阶段”。旧记录无 trace 显示“阶段耗时未采集”，若有结束时间，
总耗时仅为已有开始/结束时间差。所有日志响应（含拒绝）依 RFC 9111 §5.2.2.5 设置
`Cache-Control: no-store`，只接收 JSON；元数据过期不改写成运行失败。
阶段白名单保留 RUN 实际记录的 `duration_ms`，工具条目同时显示该次工具耗时和
`elapsed_ms` 累计耗时；缺少工具耗时就标“未采集”，不能由累计值推算。

## 本地验证与配套发布

```bash
uv run pytest tests/dify/test_operator_run_logs.py tests/dify/test_operator_run_logs_bridge.py
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/gateway
```

在自己忽略的 scratch 中从固定 Dify `1.17.1` 提交解出干净源码，排除 `.env*`，
建立独立 Git 根，再按 `deploy/dify-36/web-patches/build-web-image.sh` 的顺序应用全部补丁、
复制组件和 service。将 `aiops-operator-run-logs.browser.spec.tsx` 放入 `web/app/components/`：

```bash
pnpm exec vp check web/app/components/aiops-operator-run-logs.tsx web/app/components/aiops-operator-run-logs.browser.spec.tsx
pnpm --dir web exec vp test --project browser --run app/components/aiops-operator-run-logs.browser.spec.tsx
pnpm --dir web exec next build --webpack
```

完整构建必须沿用 canonical 脚本的 Node 24.20.0、webpack 和
`NODE_OPTIONS=--max-old-space-size=4608 MALLOC_ARENA_MAX=2 RAYON_NUM_THREADS=1 UV_THREADPOOL_SIZE=1`。
自有 scratch 同步该脚本对 `next.config.ts` 的设置：`cpus: 1`、
`webpackMemoryOptimizations: true`、`webpackBuildWorker: true`、`parallelism = 1`、
`cache = false`，清理自己 `.next/cache` 后单任务运行。低堆上限 OOM 不代表环境缺少构建能力。

Gateway、同源 bridge 与 web 补丁需要配套发布；只发前端会请求不存在的动作。
RUN 必须提供 `AgentRun.trace`。现有 `OperatorCpms.start` 已在原生预览签发时写
`publication_app_id=binding.app_id`，无需改 `_native_preview`；没有 trace 的旧账本可读，
但缺应用归属的旧记录不猜测回填。`self_service_operator_identity` 返回前已执行
`refresh_scope(..., allow_empty=True)`，日志端点复用该接缝。部署与独立验收由父任务负责。

## LOG 验收合同

```gherkin
Feature: 原日志入口可解释 Java 管家运行
  Scenario: 本人当前权限内查看成功、失败与过期运行
    Given 应用已登记为 operator 且公司账号拥有当前应用和场站权限
    When 从原日志入口刷新、翻页并打开详情
    Then 只显示当前应用本人的运行元数据、真实阶段与版本来源
    And 过期成功记录仍显示成功与结果已过期，不返回业务结果
    And 已过期的在途运行显示已过期，工具耗时与累计耗时分开呈现
  Scenario: 权限撤回后刷新
    Given 用户曾打开日志详情
    When 公司身份、场站范围或 Dify 应用访问权不再匹配
    Then 请求拒绝或列表为空且页面清除先前详情
```

LOG-01：临时 Gateway 根，生成双用户/双应用/双租户、撤权和过期记录，
调用列表/详情，核对实际账本的引用与状态、字段白名单、no-store 和数据库未写；清理临时根。
LOG-02：获准后在原 Dify 完成成功/失败试用，打开原日志入口比对同一 QA；
另用普通 app/consumer 检查原生日志，撤权后刷新，关闭浏览器。实现者的合成检查不替代此项。
