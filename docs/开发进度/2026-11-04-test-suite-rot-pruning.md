# 2026-11-04：测试模块腐烂审查与精简

**里程碑目标**：阶段收尾时审查 `tests/`，剔除腐烂部分。判定标准不是"写得啰嗦"
或"跑得慢"，而是**这条测试是否真的能失败**：恒真断言、空转参数化、只读源码找
短标记、测试自己重算期望值——它们都声称守住某个属性，实际一条断言也没执行。

**当前分支或提交**：`test/slim-rotten-python-tests`，base `9ca549685d05`。

## 已完成内容

**1. 修好 8 条假绿**（本轮真正的价值）。每条都反向验证过：改**生产代码**让它
违反属性 → 必须红 → 恢复。不是改断言看它变绿。

- `test_dify_trial_scope.py`：`hasattr(runtime, "_trial_operator_scope")` 恒真
  （构造器无条件设该属性）。改为断言 `set_trial_operator_scope` 被调用。
- `test_dual_entry_bridge.py`：`upstream.manage_requests` 这个属性根本不存在，
  `x if hasattr(...) else None` 让断言恒真。改为断言上游实际收到请求里的 `sources`。
- `test_dual_entry_bridge.py`：`assert ... or True`。
- `test_dify_workflow_trial.py`：`DataGrantStore` 没有 `all()`，`all([])` 恒真。
  改为直接查 `chat_data_grants`，并要求至少一行（"什么都没签发"也要红）。
- `test_operator_cpms.py`：测试把 `snapshot.get("rendered_report") or render_report(...)`
  抄了一遍，等于断言"重算结果 == 重算结果"。改为断言冻结值本身存在且被交付。
- `test_agent_metrics.py`：自造 dict、删 key、断言 key 不在；零生产代码参与。删除。
- `test_dify_registry_route_proof.py`：断言工具源码里出现中文措辞。改为 AST
  断言 `main()` 把"不可区分"记进 `failures` 并因此退 1。
- `test_new_language_system_copy.py`：`for language in [...] if 缺失` 恒空循环。
  改为断言四种语言都没有回落。

**2. 去掉逐字重复**。四个 `test_diag_*_contract.py` 里有三组测试体逐字相同
（同一份 controller、同一组 header 校验），抽成 `test_diag_shared_contract.py`；
`test_m2_close_contract.py` 与 `test_agent_run_records.py` 的 scope 快照测试逐字
相同，删 m2 那份（记录层单元测试归 records 文件）；`_seed_default_workspace`
两处逐字相同，移进 `tests/dify/conftest.py`。

**3. 清死代码**：25 个从未被引用的 helper，其中
`test_terminal_write_guards.py` 的 `_write_is_answered` 还引用了一个未定义的
`_answered_writes` —— 一旦被调用就是 `NameError`。另删除
`test_operator_catalog_th_km.py` 里 82 行无人调用的客户端脚手架。

**4. 收窄措辞快照**：`.env.example` 的注释断言从"整句必须出现"改为语义锚点
（如"已退役" + `diagnostic_readonly`），改措辞不红、删语义才红。

**5. 删空转**：`test_operator_catalog_vi_mn.py` 里 `_NOT_YET` 恒为空的参数化 ——
收集 0 条，永远不会失败。

## 当前分支或提交

`test/slim-rotten-python-tests`，base `9ca549685d05`。
净变化 **-377 / +331 行**，用例 4112 → 4100。

## 验证结果

| 检查 | 结果 |
|---|---|
| `uv run pytest -q -n 4`（干净 worktree） | 全绿，4100 条 |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 1458 files already formatted |
| 8 条修复的反向验证 | 改生产代码 → 红 → 恢复，逐条确认 |

主仓根目录有一个未跟踪的 0 字节文件 `0`，会让 `tests/test_repository_layout.py`
转红；干净 worktree 里该测试通过。该文件不是本轮产物，未处理。

## 明确不做的四件事

审查（四个并行 agent 各审一片）给出的建议里有四条与仓库自身规范冲突，按规范执行：

1. **不删退役墓碑**（`test_runtime_retirement.py`）。建议理由是"模块已不存在、
   扫描恒为空"；但 `docs/architecture/repository-layout.md:37` 写明这三组测试
   要继续执行。删掉是违背已记录的决定，不是精简。
2. **不按里程碑推定废弃 M2/M3 测试**（`tests/tooling` 约 2600 行）。同一份规范
   写着"不按里程碑过时推定废弃"。试运行栈是否退役需要单独的决定和验收。
3. **不删 `test_readme_architecture.py` 的模块清单**。它是规范点名的三组之一。
4. **不删 `test_operator_questions_triage.py` / `test_dify_ops_model_evidence.py`**。
   前者的目标文档仍在被跨文档引用为结论来源，后者的目标脚本仍在手册里作为可复跑
   取证工具 —— 删测试会留下无测试的资产。

## 已知缺口 / 下一步

- 仍有约 60 个测试函数在读源码文本。其中多数是**结构性守卫**（"不许手写 HTTP
  调用""所有运行时守卫必须走同一个渲染器"），它们防的是未来的编辑，行为测试
  确实看不见；本轮只处理了其中"在整份文件里找短标记"因而会假绿的那一批。
- `tests/tooling` 里试运行栈专属的测试是否随栈一起退役，需要一次单独决定。

**真实故障业务验收：未完成** —— 只改测试，无用户可见行为变化。
