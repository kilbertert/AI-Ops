# 工具与公司集成按用途归位

基线`041a1eb0`（Gateway 归位之后），分支`refactor/tooling-layout`。
`tools/`根层散落的生成器、探针、隔离实验与清单按用途分入`content`、`verification`、
`experiments/m2_close`、`inventory`；公司 Java 补丁由`java/`移入`integrations/cpms-java/`；
25个工具测试归`tests/tooling`、9个公司契约测试归`tests/integrations`。
`tools/`根层只留`README.md`与`test.sh`；不新建转发空壳，也不保留旧路径副本。

被移动的文件内容逐字不变，改动面是引用：`src/`、`deploy/m2-live-03/`、`ops/`、
`services/`的README、`.claude/skills/`脚本、`docs/agents/`运维手册与现役说明。
包内导入按新目录解析：4个探针测试的`sys.path`指向`tools/verification`，
内容编译器测试指向`tools/content`（`build_consumer_knowledge`与`merge_faq_i18n`同目录）；
`#414`六个证据守卫的清单路径改为`tests/integrations`。

## 自动化结果

| 检查 | 结果 |
|---|---|
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 通过，1086 文件已格式化 |
| `uv run pytest -rs`（全量） | **3430 passed, 9 skipped**（与 Gateway 片基线同一数量） |
| `deploy/m2-live-03/context_digest.py --check` | 一致，179 个上下文文件 |
| 镜像重建（`build-image.sh --update-lock`） | `aiops-m2-live-03-gateway:tooling-layout`，`source_sha256` 按新树重算 |
| 容器内导入 | `gateway.*`、`operator.operator_conversation`、`tools/verification/m2_live/*`、`m2_close.harness_guard` 均可导入 |
| `node .sandcastle/repo-map.check.mjs` | 通过（201 行，确定性） |
| `node .sandcastle/adr.check.mjs` | 通过（163 条记录，6 条既有 advisory 不参与门禁） |
| `node .sandcastle/conflict-surface.mjs --check` | 通过 |

失败与修复（本片在会话中断处接续，逐条保留）：

1. 全量首跑5个收集错误：`m2_live`与`merge_faq_i18n`找不到 —— 探针与内容编译器的
   兄弟导入只按脚本所在目录可见，测试的`sys.path`还指着旧`tools/`根。改指向
   `tools/verification`与`tools/content`后通过。
2. `test_the_documents_agree_on_how_many_evidence_tests_there_are`失败：`#414`
   守卫清单仍写`tests/`根路径。改为`integrations/…`新位置后通过；文档里的计数断言不变。
3. `test_gateway_context_digest_is_reproducible_and_matches_the_lock`失败：上下文摘要
   按新路径重算为`3718735069…`，锁里还是移动前的`5370006c…`。用
   `build-image.sh --update-lock`重建镜像并重写锁，`--check`复测一致。

## 边界

- 本片**未部署**、**未做业务验收**：只移动目录与引用，未改业务分支、权限、存储格式或
  错误判据，因此不新增变异测试，也不重跑真实 C 端/管家会话。
- 生产 41 上的服务目录、凭据与运行进程未被触碰；`integrations/`不进 Gateway 镜像。
- `CHANGELOG.md`、`docs/validation.md`、`docs/开发进度.md` 是归档，未回写；
  `docs/adr/`中的历史路径按记录当时的事实保留，不改写。
