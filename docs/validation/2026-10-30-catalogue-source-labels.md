# 来源版本标签：本地验证

对应[开发进度](../开发进度/2026-10-30-catalogue-source-labels.md)。分支`fix/catalogue-source-labels`，
基线`7b5d62f3`（起草`83ba786a`，期间基线前移六次：`#1184`/`#1185`/`#1186`/`#1195`/`#1196`/`#1197`；每次改动 `src/` 的都要重建锁）。本页只记本轮实际跑过的检查。

## 自动化检查（全部本地，未上生产）

| 检查 | 命令 | 结果 |
|---|---|---|
| 专项测试 | `uv run pytest tests/dify -n 4` | 753 tests / 0 失败 / 0 跳过 |
| 全量测试 | `uv run pytest -n 4` | 3441 tests / 0 失败 / 9 既有跳过（junit 统计） |
| 静态检查 | `uv run ruff check` + `uv run ruff format --check`（三个改动文件） | 通过，无重排 |
| 镜像内容摘要 | `deploy/m2-live-03/build-image.sh --update-lock` | `source_sha256=e3ff5c96d0e3b07f…`，`built_at=2026-10-30T03:36:09Z` |

新增两条用例（`tests/dify/test_self_service.py`）：

- `test_catalogue_labels_source_by_customer_and_dify_app_name`：目录返回
  `客户A / 优链充－平台全链路验收`，并断言第二次读目录不再问 Dify（应用名缓存生效）。
- `test_catalogue_falls_back_to_agent_name_when_dify_is_unreadable`：控制台读名抛错时
  目录仍 200，标签退回智能体名（`客户A / operator-node`）。

镜像摘要那一条测试（`test_gateway_context_digest_is_reproducible_and_matches_the_lock`）
本轮**先红后绿**：改动 `src/` 后摘要必然变化，按
[本地 Gateway 镜像手册](../agents/local-gateway-build.md)真实构建并 `--update-lock`
写回锁文件，随后该模块与专项一起通过。没有手改摘要。

决策记录闸门：首个提交因「`fix(dify):` 动了 2 个源文件而 `docs/adr/` 未变」被
`Workflow policy` 的 `adr.check.mjs` 拒绝。复核后判定确实欠一条记录（本片引入
「读路径里多一次跨服务读 + TTL 缓存 + 静默降级」这个形态），补
[ADR-0178](../adr/0178-catalogue-label-reads-the-dify-app-name.md)，没有用 `No-ADR:` 绕过。

类型检查：`uvx basedpyright` 在基线 worktree 与本分支各跑一次，
本分支**没有新增**错误——两边错误集合逐条相同（18 条，均为既有的 Optional 收窄问题），
仓库既有的 CI 门禁仍是 ruff + pytest。

## 未做的验证（不能当作已覆盖）

- **未部署**：改动只到本地提交，41 上的 Gateway 仍是旧版本，所以真实平台账号下
  下拉文案**没有任何线上证据**。
- **未验真实控制台读名**：单测用的是替身 `_call`；真实 `/console/api/apps/<id>` 对
  **客户工作区**的读取走的是与 `adopt_draft` 相同的调用方式，但本片没有真的读一次。
- **未验兜底的真实形态**：读不到名字时的降级只在合成数据上验过。
- 下拉里 `· V{版本}` 仍是智能体正式版本号，本次没有改动（改这处要重建 Dify web 镜像）。

结论：这是**代码与构建级**验证，不构成业务验收。

## 并行交付造成的锁文件冲突（本轮实际发生）

`deploy/m2-live-03/images.lock.json` 是 `src/` 的函数，两个同时改 `src/` 的交付必然都要改它。
本轮实测两次撞上：

1. 首次推送时兄弟工作树 `AI-Ops-site-query-seam` **未提交**地改了同一个文件，pre-push
   守卫拒绝推送（`push overlaps uncommitted changes in another worktree`）。按用户决定
   等它收尾：它随后以 #1195 合并。
2. 合并后 `src/` 又变，锁摘要再次过期 —— rebase 时在锁文件上冲突，JSON 被冲突标记打断。
   处理方式是**取主干侧的锁**（`git checkout --ours`）、继续 rebase，然后用
   `build-image.sh --update-lock` **重新构建**出当前树的值，而不是手工解决冲突或手写摘要。

结论：锁文件不能并行编辑，它必须由最后落地的一方重建；「解锁靠重建，不靠合并文本」。

## 锁文件争用在同一天内发生了三次

合并窗口里另外三个交付（#1195 站点查询、#1197 运行类型、以及未提交的 refactor/run-capability）
都在改同一个 `images.lock.json`：pre-push 守卫两次因兄弟工作树**未提交**的同名改动拒绝推送，
rebase 一次在锁文件上冲突。每次都按同一条规则处理 —— 取主干侧那份、继续 rebase、
`build-image.sh --update-lock` 重新构建出当前树的值。没有一次手写摘要或人工合并冲突文本。

## 上线与线上核对（2026-10-30）

| 事项 | 证据 |
|---|---|
| PR | [#1194](https://github.com/kilbertert/AI-Ops/pull/1194) → squash `bf6085eb`，03:48:08Z |
| CI | `verify` 首次 5m34s **失败**，重跑 4m6s 通过（见下）；`Workflow policy`、`spec-tree lint` 通过 |
| CD | run `40999387411`，12m8s success（排在 #1197 的 CD 之后） |
| Gateway 版本 | `curl http://172.18.0.1:8788/health` → `1.0.0+bf6085eb139a` |
| 部署内容 | `/opt/aiops-41/src/aiops_diagnostics/dify/self_service_api.py` 含 `source_app_name`/`tenant_name`/`app_name`，mtime `2026-10-30 11:48:08 +0800` |
| 真实读应用名 | 在 36 用同一调用路径（`GET /console/api/apps/<id>` + `X-WORKSPACE-ID`）读客户工作区 `9d2ccbac…` 的 `a57602c1…`，返回 `name=优链充－平台全链路验收` |

### CI 首跑失败是测试自身的偶发，不是本改动

首次 `verify` 在 `tests/test_h5_assistant_routes.py::test_every_declared_path_is_forwarded`
失败，断言把**全部**声明路径报成「没转发到网关」——那是一个用真 nginx + 桩起转发面的
集成测试，全部路径同时不转发意味着探测期上游根本没应答，而不是某一条正则写错。
本机连跑 3 次（`-n 0`）全部通过，改动也没碰 `ops/nginx/`；重跑同一 job 通过。
结论：**CI 环境侧偶发**，本片无回归；但这条测试的失败信息会伪装成"路由全丢"，
值得单独治理（未在本片处理）。

### 仍然没验的

- 真实平台账号打开「复制并开通」看下拉文案 —— 需要一次正常登录，本轮没有做。
- 读名失败时的降级在线上没有出现过，只有合成用例。
