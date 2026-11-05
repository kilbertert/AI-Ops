# 审查执行边界修复验证

日期：2026-11-05。基线 `64def58e`，分支 `fix/reviewer-execution-boundary`。

## 目标与判据

H1：未闭合/闭合围栏内的示例不能通过或否决真实裁决；H3：候选生命周期脚本只在容器运行；
H4：审查环境不继承 GitHub/SSH/任意宿主身份，审查修改 Git ref 不触及源仓库。
实现见 ADR 0229；操作见[审查手册](../agents/reviewer-execution.md)。
这是内部执行工具变更，合成脚本可复现安全边界，不属于充电业务验收。

## 已执行

- `review-boundary.check.ts`：10 个裁决输入，含原 H1、两轮引用/冲突、CRLF、闭合与未闭合围栏。
- 用假 GitHub/Enterprise/SSH 变量驱动真实 provider 过滤；独立 clone 无 alternates、无 origin。
- `--docker`：实际 SDK 创建容器，运行合成 `npm ci` 的 postinstall；回执确认容器内执行、
  宿主假秘密及 GitHub/Enterprise 凭据不可见。只读宿主挂载拒写，容器 `update-ref` 不改变源 ref。
  通过 SDK `sandbox.run` 执行确定性 agent 夹具，最终裁决回读成功；无需真实模型额度。
- 初版检查尝试把副本 ref 指向未复制的后继对象而失败；订正为候选已包含的父提交后通过。
  该失败属于验收驱动，不放宽隔离断言。
- 目录/文档现有检查 25 通过；ruff、shell 语法与 AFK workflow 检查通过。
- TypeScript 受影响模块静态检查通过；wheel/sdist 构建通过。
- 全量 Python 回归：4239 通过、8 跳过，耗时 232.74 秒；跳过均需外部 GitLab 环境。
- 全量 CI 与独立 Hacker 结果：待验证。

## 凭据与未验范围

凭据轮换属于独立操作任务，本次只交付执行边界修复，不据此声明历史事件关闭。
供应商型号、长期费用收益与全量真实 ralph 业务批次未验证。未完成业务验收。
