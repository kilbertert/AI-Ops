# Gateway 设置来源表（#973）

- 里程碑目标：`GatewayServerSettings.from_env` 的 76 个字段从手写
  `_env(...) or _file_value(...)` 链收敛为 `SETTING_SOURCES` 一张声明表，
  「一个设置从哪里读」有唯一权威并机械核对。
- 已完成：表驱动 `from_env`；`--config` 三处读者统一「键存在性」判据与
  Option A 显式点名拒错；`.env.example` 补齐 30 个早已生效但漏登记的键、
  19 个 `file=False` 键行尾标 `# 仅进程环境`（镜像测试双向核对）；m2-live-03
  模板与 checks.sh 的「空值继承」注释按新语义更正；runbook 两文件分工指向表。
- 对外行为变化（issue 选项的显式决定）：统一「缺失才回退」——`KEY=` 空串
  是值不是「没配」（选项 d）；显式点名的 server config 缺失或非 0600 即
  `PrivatePathError` 启动失败（Option A）。实测本机全部配置文件无一处
  `KEY=` 空串依赖回退。
- 当前分支/提交：`refactor/gateway-setting-sources`，基线 `224ac847`。
- 验证结果：`tests/gateway/test_setting_sources.py` 223 条整表参数化 +
  `file=False` 独立台账 + `from_env` AST 守卫；六条变异探针全红（首轮
  `file` 列翻转漏检，台账补上后转红）；`verify-aiops-gateway` 一次性
  数据根全过（`dist/verification/gateway-973/`）。ADR-0230。
- 已知缺口：41 上 `gateway.env`/`production.env` 的实际键分布未逐一核对
  ——表记录的是代码实际读的位置；部署若按 `.env.example` 旧注释把
  introspection 凭据写进 `production.env`，那些键本来就从未生效。
- 下一步：随正常 CD 部署；迁移时核对 41 两份文件的键是否都在各自生效的
  那一侧（`SETTING_SOURCES` 的 `file` 列就是判据）。
