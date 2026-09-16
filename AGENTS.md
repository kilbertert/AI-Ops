# AI-Ops 仓库开发共识

本文件只补充 AI-Ops 项目的约束；服务器级 `AGENTS.md`、Git/PR/CI、凭据和安全规则仍然优先适用，不能被本文件放宽。

## 文档语言

- 面向工程师、部署人员和业务验收人员的文档必须以中文为主：`README.md`、`docs/`、`ops/README.md`、配置模板注释和验收记录均适用。
- 命令、环境变量、JSON 字段、代码标识、协议名称和必要的英文原文保持不变，并在首次出现时给出中文解释。
- 业务 SOP 以中文为准；英文技术术语只能作为辅助，不得替代中文结论、限制条件和操作说明。

## 里程碑同步（强制）

每个有可交付结果的开发里程碑，在提交或创建 PR 前必须同步：

1. 更新 `docs/开发进度.md`，记录里程碑目标、已完成内容、当前分支或提交、验证结果、已知缺口和下一步。
2. 更新 `docs/validation.md`，记录新增的自动化检查、真实环境验证、失败与修复；没有真实故障案例时必须明确写出“未完成业务验收”，不得把 fixture 或模型调用写成真实故障结论。
3. 文档中的测试数量、CI 结果、制品地址和状态必须与本次提交实际证据一致。未知内容写“待验证”，不得猜测填充。
4. 若里程碑改变用户入口、配置、部署方式或安全边界，同时更新 README 和对应部署/架构文档。
5. 做操作知识自检（见下）；本轮实际执行过的运维步骤若在仓库内无文档，先沉淀再宣告完成。

### 操作知识自检（强制）

跨 agent 经验必须落进 repo 才算共享——只存在于会话线或 agent 记忆的操作步骤，其他
agent（Codex / AFK / 下一个会话）完全不可见，会把"已有现成方法"误判为"需要人提供
条件"。原则见 `mem-20260910-ranlei-001`；41 运维手册
（`docs/agents/env-41-runbook.md`，PR #256）即因此缺口而补齐。

每轮收尾前，对**本次实际执行过的运维命令**做一次 grep 自检：

```bash
# 以本次改动涉及的主机名或服务名替换 <host>；命中为空即说明只存在于记忆
grep -rn "<host>\|ssh \|scp \|redis-cli\|systemctl restart" docs/ ops/ README.md 2>/dev/null | head
```

- 命中为空 → 该操作步骤未入仓，先写入 `docs/agents/` 对应手册（新建或补进既有手册），
  再提交里程碑。
- 文档只写**方法与位置**，不写口令、令牌、会话明文等凭据；凭据一律运行时从受控位置
  现场读取。提交前用 grep 复核无明文泄露。
- 判断标准是"另一个 agent 能否照着执行"，不是"我是否记得"。结论（当前状态）与方法
  （如何操作）分开沉淀：前者进 `current-delivery-state.md`，后者进 `docs/agents/` 手册。

回答当前交付状态、修改 41 数据或进行真实验收前，先读
`docs/agents/current-delivery-state.md` 与 `docs/validation.md` 首节；以当次可复现证据更新结论。

**动手改 41 或做真实端到端验收前，按 `docs/agents/env-41-runbook.md` 执行**——它记录
41 的访问方式、源码部署与 sha 校验、Agent/快捷动作的生产代码路径创建、真实会话与
订单的获取方法、公网验收命令和排查入口。这些是可复用操作步骤，不需要从会话历史推断。

## 业务边界

- 第一版只允许诊断和证据交付，禁止重算、补发、退款、修改订单、修改配置、重放消息、消费游标和重启服务。
- 没有工程师确认的真实故障结论时，只能报告自动化验证、生产只读回放或真实模型验证，不能宣称业务准确率已经验收。
- 新增动作能力必须另行设计审批、权限和审计账本，不能在诊断工具中顺手加入。

## 验证与交付

- 代码、配置、打包或文档流程变更都要运行与风险匹配的 formatter、静态检查、测试和构建检查。
- Windows 便携包必须在 Windows runner 上构建，并从最终 ZIP 的解压目录验证，不能只运行源码目录或 `dist/` 目录。
- 每次 PR 只解决一个逻辑任务；AI 审查是建议，不替代确定性 CI 和人工判断。

<!-- afk-bootstrap:managed:start -->
## AFK workflow gate

For idea or planning work, read `docs/afk-workflow.md` and the applicable
files under `docs/agents/` first.

`/grill-with-docs` ends only when its frontier is empty: report
`GRILLING_COMPLETE`, summarize the shared understanding, ask the user to
confirm it, and stop. Confirmation completes grilling only. Wait for the user
to explicitly invoke `/to-spec`, `/to-tickets`, `/implement`, or
`/implement-spec`; do not enter another phase automatically. Multi-session
work uses `/to-spec` then `/to-tickets` before implementation.
<!-- afk-bootstrap:managed:end -->
