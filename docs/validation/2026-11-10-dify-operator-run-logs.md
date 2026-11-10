# Dify 管家运行日志本地验证（LOG #1455）

日期：2026-11-10 UTC。基线 `3da9be7f`，分支 `feat/dify-operator-run-logs`。
验收输入：已批准的 #1452 `order-feedback` 规格；可执行合同和 LOG-01/02 步骤见
[运行日志说明](../agents/operator-cpms/run-logs.md)。本轮只本地实施和提交，不 push/PR/部署。

新增长期检查拦截跨 app/用户/租户及撤权泄露、分页越界和把快照当日志输出；
判据来自规格的本人可见范围与元数据白名单，既有测试没有原日志入口及该读模型合同。

## 实测结果

| 检查 | 结果与边界 |
|---|---|
| 两份新增 Python 测试及目录/README 检查 | 最终 53 passed；日志 API 使用真临时 Gateway/SQLite，bridge 使用真 HTTP，身份/上游为合成替身 |
| 覆盖率 | 两个新模块合计 95%；Dify 路由 93%，执行投影 100%，含分支统计；不以此代表业务准确率 |
| 授权变异 | 临时插件分别移除应用筛选、权限指纹筛选；两个变体均因混入记录在可见集断言失败，未改维护源码 |
| 扩展回归 | 344 passed、1 failed；失败是既有自助发布用例 `SETUP_EXECUTOR_UNAVAILABLE`，该用例单独复验 1 passed。回执有效期为 120 秒，整组耗时 1819 秒；资源争用可能导致过期，未篡改 TTL 或检查 |
| Ruff | 全仓 check 与 format --check 通过，最终 1668 文件格式通过 |
| Python 交付 | wheel/sdist 构建、compileall、uv pip check 通过；新模块进入包，无新依赖 |
| Gateway skill | 一次性根启动；8 项 health/匿名拒绝/公开规范/退役入口检查通过，进程与临时根已清理 |
| Dify 补丁 | 固定 1.17.1 上游干净解包后全部现役补丁及 14 号日志补丁以 fuzz=0 应用；构建脚本语法通过 |
| 前端静态 | 两个新组件/测试文件 `vp check` 通过，无格式、lint、类型错误 |
| Chromium | 原完整配置两次依赖预打包/会话超时，未执行；精简临时配置初次 4 passed/1 timeout，随后分页单独通过，最终整组 5 passed（4.30 秒） |
| Next webpack | 已尝试；2.5 GiB JS 堆上限下 OOM，退出 1，未产出可部署 web 制品。不能标构建通过 |
| 生命周期 | `dev-worktree audit` 通过，32 个 worktree，其他工作未改动 |

收尾新增中文文档后的目录/README 检查另跑 25 passed；没有用该结果替代完整回归。

证据留在本工作树忽略目录 `dist/verification/`：`logs-pytest-final.xml`、
`logs-coverage.json`、`logs-regressions.log/xml`、`logs-publish-recheck.log/xml`、
`logs-mutation-scope.log`、`logs-mutation-app.log`、`logs-browser.log`、
`logs-next-build.log`、`gateway/aiops-gateway.json`、`worktree-audit.txt`。
Python 制品在 `dist/logs-build/`。关键源码 SHA-256：

- Dify 路由：`a5613213f4b157ab0e09a60a8467a85ca20d47df70c02a0b6a6c97da7738aec2`
- 执行投影：`c9293e2dcbf190fdd1031d6f83d6a3b01a3a0a55a3adcb27eed43f92fc0c8857`
- 日志组件：`26439719fb8b9ab6618c22638a397504174fdd01fb565d982f45c76200cc9dbd`

精简 Chromium 配置仅加载 React、同版本 Playwright、单个日志测试及实际 dify-ui Button，
不加载全局 Tailwind 样式；证明交互和请求，不证明完整主题布局。配置在自己 scratch 的
`web/vite.logs.config.ts`；命令 `pnpm --dir web exec vp test --config vite.logs.config.ts --run`。

## 已知缺口与交接

没有生产访问、真实模型/Java run、真实公司登录、原 Dify 页面整链验收或独立 Hacker。
**未完成业务验收**。未跑全仓 pytest 或完整镜像构建；受限 web 构建失败需父任务补跑。
Python 3.13 检查出现既有 Starlette 弃用与 fixture 数据库 ResourceWarning，未扩展本任务修复范围。

依赖 RUN #1453 提供 `AgentRun.trace`；本分支通过 `getattr` 支持旧结构并用合成 trace 检验
白名单。真实新 trace 的落库读取及原生预览必须与 RUN 一起复验。父任务负责 `_native_preview`
把实际应用 ID 带入签发；缺 `publication_app_id` 的历史记录不回填猜测。
Gateway、bridge、web 必须配套发布；本轮未编辑父任务的 agent_runs/operator_cpms/gateway_api。
独立验收、完整 Dify web 构建、CI、人审与发布由父会话完成。

操作知识自检：本次仅运行本地构建与测试，方法已写入运行日志说明；未执行 SSH、生产重启或数据操作。
