# 确定性文案出口语言解析日志验收

日期：2026-11-12。分支 `feat/log-localized-copy-language`，base `7d51bd35`，PR #1482。
起因：管家端 App 反馈「选了中文、回答中文、预设问题英文」。排查坐实客户端真的发了
`Accept-Language: en`（`chat_dify_runs.language` 分布 + 英文持久回复块），服务端按合同
如实给英文。本 PR 只加观测：下次同类反馈一条日志即可定位，不用再翻库反推。

## 判据与范围

- 判据来源：调查结论本身——FAQ、starters、shortcuts、banner 六个面只把解析语言用在
  当场挑文案，服务端无任何记录；chat/trial/analysis 路径的语言已有 run 行持久化
  （`chat_dify_runs.language` 等），再记日志是纯噪音。
- 因此是单独依赖 `localized_copy_language` 挂在上述六个端点，而不是给
  `request_language` 加日志（其余 11 条路径维持不换）。
- 日志行：`localized_copy_language path=… accept_language=… resolved=…`，
  `aiops.gateway` INFO，经既有 `gateway_logging` root handler 进 journal（#636 已修）。

## 验证证据

- 新增 `test_localized_copy_language_is_logged`（tests/conversation/test_faq_gateway_api.py，
  parametrize 六端点）：caplog 断言**每个面**各产生一条 `aiops.gateway` 记录，
  字段含原始头与解析值（`en→en`、缺省→`zh`）。拦截的具体错误：重构丢 `_LOGGER.info`
  或把任一端点换回不记录依赖。只断言日志不断言响应码——依赖先于处理器执行，
  starters/shortcuts/banner 在最小夹具里缺下游件时日志照样落地。
- `pytest tests/conversation/test_faq_gateway_api.py tests/publication/test_shortcut_api.py
  tests/gateway/test_banner_api.py` — 68 passed（worktree 独立 venv，7d51bd35 base）。
- `ruff check` / `ruff format --check` — clean。
- 无对外行为变化：路由、鉴权、响应形状未动，按 AGENTS.md 不要求 verify-aiops-gateway。

## 未验范围

- 生产 journal 的实际落行：依赖 #636 日志修复已在 41 部署（部署时随本候选一起生效），
  本地 caplog 只证明记录发出与字段形状。
- 客户端侧的 `storage.lang` 写入路径不在本 PR（App 属公司仓库，另走沟通）。
