# LOG #1455 耗时与过期状态补验

2026-11-10 UTC；分支 `feat/dify-operator-run-logs`，本轮起点 `7c4bcf5a`，
本地 `origin/main` 仍为 `3da9be7f`。没有引入 RUN task 分支或版本 1.4.0，
待其 PR 合并后再按父任务安排集成 main。本轮仅本地提交，不 push/PR/merge/deploy。
本记录补充[首轮验证](2026-11-10-dify-operator-run-logs.md)，不改写首轮失败证据。

## 修正与补审

- trace 白名单补 `duration_ms`；工具条目展示真实该次耗时，累计 `elapsed_ms` 单列。
  缺 duration 明说“未采集”，不能用累计值伪造；总耗时优先最后真实事件。
- 已到期的 `issued/running` 在只读响应中投影为 `expired`；成功、失败终态不重写。
  数据库未写入，业务结果时效仍单独展示，未到期运行仍为 `running`。
- 确认 `self_service_operator_identity` 经 `authenticated_operator_console` 返回前已调用
  `refresh_scope(..., allow_empty=True)`；新增实际目录范围改变、resolver 身份不变的拒绝检查。
- 确认现有 `OperatorCpms.start` 已写 `publication_app_id=binding.app_id`；无需修改
  `_native_preview`。首轮将该项列为待修改不准确，运行手册已纠正。
- 补审固定 bridge 动作、字段白名单、已登记 app、本人/租户/权限筛选、分页上限、no-store、
  过期元数据与业务回读分离，以及 React effect 清理和日志 key；未扩展数据或全局配置边界。

最小新增检查针对已确认的 duration 丢失、过期在途误显 running，以及目录撤权后沿用旧身份；
判据来自本次用户确认和既有授权接缝，首轮未覆盖这三种情况。

## 实际检查

| 项目 | 本次结果 |
|---|---|
| Python / bridge 定向检查 | 最终 35 passed，14.93 秒；含终态保留、在途到期/未到期、只读数据库、末事件耗时、目录实时撤权 |
| Dify 原完整浏览器配置 | 6 passed，20.49 秒；使用真实 Chromium、全局样式与实际 dify-ui Button，管理 API 为合成替身 |
| 前端 `vp check` | 两文件通过，无格式、lint 或类型错误 |
| Ruff | 全仓 check / format --check 通过 |
| Python 制品 | wheel/sdist、依赖一致性、修改文件 compileall 通过，仍为当前分支 1.3.1 |
| 完整 Next webpack | **通过，退出 0**；编译 9.1 分钟，完成 page optimization / build traces，`/app/[appId]/logs` 进入路由产物 |

构建严格沿用 canonical `build-web-image.sh` 已证明设置：Node **24.20.0**、
`NODE_OPTIONS=--max-old-space-size=4608`、`MALLOC_ARENA_MAX=2`、`RAYON_NUM_THREADS=1`、
`UV_THREADPOOL_SIZE=1`、webpack；清理自有 `.next/cache`，Next `cpus: 1`、
`webpackMemoryOptimizations: true`、`webpackBuildWorker: true`、`parallelism=1`、`cache=false`。
单任务在自有 scratch 构建，无其他 web 构建并行。上一轮 2.5 GiB 堆 OOM 是构建参数偏离，
不能表述为能力缺失；本次已解决。未构建/部署 Docker 镜像。

产物：`.scratch/dify-logs/web/.next/standalone/web/server.js`，
BUILD_ID `E63ruG6aGlWqxJkc4YrCD`；已在编译 JS 核到“工具耗时”标记。
上游动态页面因 `headers` 无法静态渲染的提示仍有，构建最终按动态路由生成且成功退出。
Next 上游构建默认跳过全仓 TS 检查；本轮修改文件由独立 `vp check` 检验，不声称上游全仓 TS 已通过。

证据留在忽略的 `dist/verification/`：`logs-followup-python-final.xml/log`、
`logs-followup-web-check.log`、`logs-followup-browser.log`、`logs-followup-next-build.log`、
`logs-followup-package.log`、`logs-followup-ruff.log`。源码 SHA-256：
执行投影 `3cdcd80dcbcc39ac0788046c400fd0d992e6ecc78aec320204a0191032eb58e5`；
组件 `d6da8c0d56473c0a5cc3d95d025e0e7d965faf00370a940e53e3f1b7ca13e4a3`。

未运行全仓 pytest、远端 CI、真实 Dify/Java 整链或独立 Hacker；**未完成业务验收**。
尚未合入 RUN 的实际 trace 持久化，跨任务集成仍待后续 main；现有合成合同验证不替代该项。
本轮没有生产访问或 SSH 操作，实际本地构建方法已同步[运行手册](../agents/operator-cpms/run-logs.md)。
