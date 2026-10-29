# Thin Harness 清理与目录梳理验证

日期：2026-10-28 UTC；分支 `refactor/thin-harness-layout`，基线 `55a284d6`。
本轮验证的是三处无生产消费者的 Python 兼容包装删除，以及目录/依赖调查。

## 自动化证据

| 检查 | 实际结果 |
|---|---|
| 修改前配置/HTTP/Hybrid 基线 | 80 passed |
| 删除引用复核 | src、tests、tools、packaging 中旧类型/属性/方法引用为零；历史文档不改写 |
| 首次全量测试 | 4235 passed、9 skipped、1 failed；唯一失败是镜像内容锁差异 |
| 修复后全量 `uv run pytest -rs` | 4236 passed、9 skipped、45 warnings，167.08 秒 |
| 静态与依赖 | `ruff check .`、`ruff format --check .`、`compileall src`、`uv pip check` 通过 |
| Python 构建 | `uv build` 产出 wheel 与 sdist；检查 wheel 中旧包装已删，现役运行模块和旧 env 回退仍在 |
| 镜像 | 用现有 `build-image.sh --update-lock` 真构建，禁网只读容器成功导入 Gateway/现役配置 |
| 内容锁 | `context_digest.py --check`：199 个文件，`match: true` |
| 工程文档 | 相对链接、代码围栏、空白、ADR 状态/替代项检查通过 |
| 工作流与地图 | policy 与 repo-map 确定性检查通过 |
| 规格 | `spex spec lint`：0 error、53 warning；既有覆盖缺口和漂移没有在本轮补齐 |

9 项跳过：8 项缺公司 GitLab 身份锚点，1 项 operator 语言目录已无可测试缺口。
没有删测试、放宽断言或改跳过规则；原配置解析顺序、旧 env 回退、令牌有效期与凭据脱敏继续验证。
全量日志位于任务 worktree 的忽略目录 `dist/verification/pytest-final.log`。

## 失败与修复

源码改变后，旧镜像锁摘要 `9e1567c1…` 与实际 `e0e5c87c…` 不符。
本机没有 `python:3.13-slim` tag，按已有锁中的基础镜像 digest 拉取，
由仓库构建脚本生成镜像身份并写回锁；未手填构建结果。之后全量重跑通过。

验证镜像 tag：`aiops-m2-live-03-gateway:thin-harness-layout`；
镜像 ID：`sha256:37804074e04195f21bc8b93317ff8b04fb35e098ea4b81c6d862715a1a79685f`；
构造上下文：`e0e5c87c872cf8766649d0f7a5346318aacf66dbe4d1a4d2e78a7a859c1b6d63`；
构建时间：`2026-10-28T14:12:44Z`。方法见[本地构建手册](../agents/local-gateway-build.md)。
这是未提交工作树的本地验证制品，不能作为已合并生产制品部署。

## 真实环境与限制

未完成业务验收：没有调用真实订单、公司登录、模型、Dify 或 Java 节点整链。
没有关闭旧入口、移除生产适配器或删除运行数据。Gateway 外部行为未改，
本轮容器检查只证明导入与配置，不冒充 Gateway HTTP 技能验收。
未扫描仓外 Python 消费者；目录迁移方案不是已落地的新目录。
未提交/推送/创建 PR；远端 CI、Devin Review、人工评审、合并和部署均未发生。
