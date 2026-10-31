# 提示词生命周期：合并、节点部署与 41 上线验收（2026-10-31）

分支 `feat/prompt-lifecycle-final` → PR #1232 → squash 合并 **`05e82e8e`**（2026-10-31T09:01:24Z）。

## 一、合并前的基线对齐

三次对齐受保护基线，每次都在**最终树**上重建 `images.lock.json`：

| 合并 | 主线 | 全量 |
|---|---|---|
| `ad11fe91` | #1238 | 3798 passed / 9 skipped / 0 failed |
| `3a381923` | #1239 | 3803 passed / 9 skipped / 0 failed |
| `51b9ff95` | #1240（受管工作流页） | 3803 passed / 9 skipped / 0 failed |

`context_digest.py --check`、`source_digest.py --check` 在最终树均 `match: true`；
`uv_lock_sha256` 一致；`ruff check` / `ruff format --check` 通过（452 文件）；
CI（verify / Workflow policy / Spec Tree）全绿；ADR 检查通过；无未解决评审线程。

`verify_prompt_lifecycle.py --out …` 在 `07f63c56`：**46 项 / 0 失败，`dirty=false`**。

## 二、CD 第一次失败：节点制品闸门（非本次代码问题）

CD run `41186947981` attempt 1 失败于 `deploy/deploy-41.sh` 的「节点制品闸门」：

```
deploy-41.sh: 网关不能先于节点上线。
最后一次改节点源码的是 5ab4e4cf6a89，而 41 上的节点是 ae290a0d-4e2e23a6a06a，早于它。
```

独立核实这不是本 PR 的回归：

- `git log -1 05e82e8e -- services/execution-node` → `5ab4e4cf`（#1239 改的 4 个 Java 文件）
- `git diff 5ab4e4cf 05e82e8e -- services/execution-node` → **空**
- 闸门读的是**运行中容器**的 `AIOPS_NODE_BUILD`，不是 `runtime.env`（ADR-0175 无灰度窗口）

期间唯一的 self-hosted runner 被一个孤儿 `Agent Update Branch` job 占用约 40 分钟，
该 job 于 09:29:00 按自身 timeout 自行 `cancelled`（未代为取消）。

## 三、节点先行部署（由双端开通会话执行）

节点侧完成后独立复核：

- 41 上 `AIOPS_NODE_BUILD=5ab4e4cf6a89-6ffad7414d2b`，容器 `running`，`StartedAt=09:33:53Z`
- jar `sha256=6ffad7414d2b…` 与 `images.lock.json` 的 `node.sha256` **逐字符一致**
- `source_digest.py --check` → `4f64fbe0c254…`，与**从 `5ab4e4cf` 提交的树重算**的摘要一致，
  也与 main 当前源码摘要一致（`files=20`）

补充（由执行会话确认，本会话未独立复验）：光换 jar 不足以让节点上报能力 ——
capability 上报要经 41 宿主的回调 nginx，而 `/etc/aiops-node-runtime/gateway-callback.conf`
仍是旧渲染，缺 `location = /v1/internal/execution-node/capabilities`，上报一律 404。
按新模板重渲染并重启 `aiops-node-callback` 后，探针由 404 变 422（空体鉴权拒绝 = 路由已通）。
重渲染时**必须保留宿主已有的 `lua_package_path` / `lua_package_cpath`**，模板里没有这两行，
覆盖后 nginx 直接起不来。

⚠️ 遗留记录问题：`images.lock.json` 的 `node.source_commit` 在 main 上是占位符
`pending-merge:fix/dual-entry-runtime-compatibility`（由 `5ab4e4cf` 引入）。
jar 与源码摘要两项都指向 `5ab4e4cf`，所以**制品身份本身可核**；但该字段不可当作提交号使用，
建议后续用独立小 PR 改正。

## 四、CD 重跑与上线（attempt 2）

节点跟上后，由双端开通执行会话 `gh run rerun 41186947981`；**attempt 2 全部步骤 success**。
（本会话观察到的是该重跑的进行中状态，重跑由对方发起，不是 main 推进自动触发。）

部署后核对：

| 项 | 结果 |
|---|---|
| `/health` | `{"ok":true,"version":"1.0.0+05e82e8ec54e"}` —— 与合并提交逐字符一致 |
| 已部署 `src/aiops_diagnostics` | 142 个文件与 `05e82e8e` 的树**逐字节一致** |
| 唯一差异 | `__init__.py` 的 `__version__` 由部署脚本注入（设计如此） |
| 依赖漂移检查 | `pyproject.toml` 字节不同但**依赖声明逐项相同**（部署脚本自判）；`uv.lock` 完全一致 |
| 服务状态 | `active`，`NRestarts=0`，启动后无 warning 级日志 |
| 端点冒烟 | `/health` 200、`/v1/openapi.json` 200、无凭据 `/v1/faq/recommendations` 401（鉴权仍生效） |

## 五、41 真实用户端到端探测（runbook §5.6）

探测前确认三张队列表无 `queued`/`running` 作业；`--accept-live-app` 执行；
探测后对账三张表仍为空（无新增 failed）。

```
✅ zh  /v1/faq/recommendations  HTTP 200  served=zh  n=28
✅ zh  /v1/shortcuts            HTTP 200  served=zh  n=3
✅ en  /v1/faq/recommendations  HTTP 200  served=en  n=28
✅ en  /v1/shortcuts            HTTP 200  served=en  n=3
```

判据取「HTTP 200 **且** `served == 请求语言` **且** 非空」，不是只看 200。
会话令牌与凭据全程未打印。

## 六、仍未完成（不宣称为通过）

- **受管 UI 页面闭环**：用将要部署的受管 Chatflow 重做「编辑 → 预览 → 发布 → 恢复」未执行。
  此前的 119/119 用的是旧注入按钮，不能代表本版本。
- **固定 32 次真实模型样本**（两批 8 场景 × 2 风格）未执行。上一轮 16 格为 15/16，
  简洁账单把片段放进行内被拒，该失败保留，不以后续重试覆盖为全绿。
- **两端隔离页面的本版本复跑**未执行。

**结论：代码已合并并上线 41，技术链路已验；业务验收（页面闭环 + 固定模型批次）仍未完成。**
