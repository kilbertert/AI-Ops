# 2026-11-04：测试模块腐烂审查与精简

**里程碑目标**：在一次阶段收尾里，把 `tests/` 里"腐烂"的部分——不驱动任何行为的
源码文本探测、恒真断言、空转参数化、逐字重复——清出来，同时**修好那几条声称守住
属性却从不执行的断言**。精简不是目的，去掉假绿才是。

**基线**：主仓 `9ca54968`，4112 条用例，2898 个测试函数，77516 行。

## 已完成内容

| 类别 | 处理 |
|---|---|
| 假绿（声称守属性，实际零执行） | 修 8 条，见下节 |
| 逐字重复 | 合并 4 份 controller 契约测试、2 份 scope 快照测试、2 份 `_seed_default_workspace`、2 份 TTL 断言 |
| 空转 | 删 1 条零收集的参数化（`_NOT_YET` 恒空） |
| 恒真 | 删/改 5 条（`all([])`、`hasattr`、`len(A) > len(B)`、`or True`、常量与自己比较） |
| 死代码 | 删 25 个未被引用的 helper（含一个会 `NameError` 的） |
| 措辞快照 | 10 条 `.env.example` 注释断言收窄为语义锚点 |

净变化：**-377 / +331 行**，用例 4112 → 4100。

## 修好的假绿（每条都做过反向验证）

反向验证的做法是**改生产代码**，不是改断言；改完必须红，然后恢复。

| 位置 | 原来的形状 | 现在钉的 |
|---|---|---|
| `test_dify_trial_scope.py` | `hasattr(runtime, "_trial_operator_scope")` —— 构造器无条件设了该属性，恒真 | setter 被真正调用 |
| `test_dual_entry_bridge.py` | `upstream.manage_requests` 不存在 ⇒ `forwarded is None` 恒真 | 上游真的收到的那条请求的 `sources` |
| `test_dual_entry_bridge.py` | `assert ... or True` | 状态码判据（删掉恒真那行） |
| `test_dify_workflow_trial.py` | `DataGrantStore` 没有 `all()` ⇒ `all([])` 恒真 | 直接读 `chat_data_grants` 表，并要求至少有一行 |
| `test_operator_cpms.py` | 测试自己重算 `rendered_report`，于是"冻结不被改写"永远成立 | 冻结值本身，缺失即红 |
| `test_agent_metrics.py` | 构造 dict、删 key、断言 key 不在 —— 没调用任何生产代码 | 整条删除 |
| `test_dify_registry_route_proof.py` | 断言源码里有中文措辞 | AST：`main()` 必须把"不可区分"记进 `failures` |
| `test_new_language_system_copy.py` | 遍历恒空集合，零断言执行 | 断言四种语言都**没有**回落 |

## 明确没有做（以及为什么）

- **不删退役墓碑**（`test_runtime_retirement.py`）。审查建议以"模块已不存在、扫描
  恒为空"为由删除，但 `docs/architecture/repository-layout.md:37-45` 写着这三组
  测试要继续执行，删了就是违背已记录的决定。
- **不按里程碑推定废弃 M2/M3 测试**。同一份规范写"不按里程碑过时推定废弃"。
  `tests/tooling` 里约 2600 行测的是试运行栈与 M3 验收工具，它们是否退役需要一次
  单独的决定与验收，不是这次精简的副作用。
- **不删 `test_readme_architecture.py` 的模块清单**。它是 `repository-layout.md:37`
  点名的三组之一。
- **不删 `test_operator_questions_triage.py`**。它确实在断言文档措辞，但那份文档
  是会跨团队转发的结论记录；删除它需要先把该文档退役，顺序不能反。
- **不删 `test_dify_ops_model_evidence.py`**。目标脚本仍在
  `docs/agents/dify-ops-first-slice/model-acceptance.md` 里作为可复跑的取证工具，
  删测试会留下无测试的脚本。

## 验证结果

- 全量：`uv run pytest -q -n 4` 全绿（4100 条），在干净 worktree 里
  `tests/test_repository_layout.py` 同步通过。
- `uv run ruff check .` / `ruff format --check .` 全绿。
- 8 条修复各自做了反向验证（改生产代码 → 红 → 恢复）。

**真实故障业务验收：未完成** —— 本轮只改测试，无用户可见行为变化。

## 已知缺口

- 剩余仍在断言源码文本的测试（约 60 个测试函数）大多是**结构性守卫**
  （"不许手写 HTTP 调用""守卫必须走统一渲染器"），它们防的是未来的编辑，
  不是当前行为；逐个改写成行为测试可能得不偿失。本轮只处理了其中在整份文件里
  找短标记、因而会假绿的那一批。
- 主仓根目录有一个未跟踪的 0 字节文件 `0`，会让 `test_repository_layout.py` 转红。
  它不是本轮产物，未处理。

## 复核轮追加（Devin Review 四条）

四条全部成立，都是同一形状：**断言范围比它声称的大**。逐条收紧并各做一次反向验证。

| 文件 | 漏洞 | 收紧后 | 反向验证 |
|---|---|---|---|
| `test_diag_shared_contract.py` | 整份 controller 只找一个 `validateToken(`；删掉任一端点的校验，其余端点仍命中 | 按 `@GetMapping` 切成端点体，**每个端点**都要有 header + 校验 + 401 | 删 `/device` 的校验 → 红 |
| `test_dify_login_page.py`（redirect） | `"redirect_url"` / `".redirect"` 在页面别处也能命中 | 解析提交请求体，要求其中有 `redirect` 字段 | 删 `redirect: redirect` → 红 |
| `test_dify_login_page.py`（刷新） | `page.count("refreshCaptcha(")` 把**函数定义**算进 3 次 | 只数调用行（排除 `function ` 行） | 删 `.catch` 分支的刷新 → 红 |
| `test_dify_registry_route_proof.py` | 任意带「不可区分」的 `.append` 都算数 | 限定接收者是 `failures` | 改成 `print("WARN …不可区分")` → 红 |

同一轮订正里程碑记录的行数：`git diff --numstat` 实测 **+331 / -377**，原记 +178。

> **交付事故记录**：这四处修复最初与一个后来被 `revert` 的提交合在一起，
> 重建分支时只救回了原提交，修复因此一度没进 `main`。合并后在 `origin/main` 上
> 逐个 grep 才确认，随后以独立分支补交。教训：改动要各自独立成提交，
> 与需要撤销的提交混在一起会让撤销把它一起带走。
