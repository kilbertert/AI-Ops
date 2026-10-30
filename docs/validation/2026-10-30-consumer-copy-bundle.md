# 租户文案包：本地验证记录

日期：2026-10-30（UTC）。分支`feat/consumer-copy-bundle`，基线`ebc56d5d`。
本记录是**提交前**的本地验证；远端 CI、合并与部署不在本记录内。
目标与缺口见[开发进度记录](../开发进度/2026-10-30-consumer-copy-bundle.md)，
取舍见 [ADR-0185](../adr/0185-consumer-copy-is-a-published-bundle.md)。

## 环境与范围

开发机、本任务 worktree、本地 Docker（仅用于重建内容锁）。未访问 41、未发布任何真实租户
文案、未改生产数据；镜像未推送任何注册表。

## 自动化检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check .` | 通过 |
| 格式检查 | `uv run ruff format --check .` | 通过，1142 files already formatted |
| 全量测试 | `uv run pytest -n 4` | **3528 passed / 9 skipped**，170.64s |
| 本片专项 | `uv run pytest tests/consumer/test_consumer_copy.py` | 19 passed |
| 内容锁 | `deploy/m2-live-03/build-image.sh --update-lock` | 按新树重建，`built_at=2026-10-30T07:51:47Z` |

九条 skip 全部是既有环境跳过（`AIOPS_GL_CA` 未配置、playwright 解释器缺失等）。

**一次需要记下的读数**：全量第一次跑 `-n 8` 时出现 3 failed + 3 errors（集中在
`tests/execution/test_workflow_trial_release.py` 等），单跑该文件 9 passed，改 `-n 4` 复跑
全绿。判断为并行执行下的资源竞争（那一轮耗时 743s，是正常值的四倍），不是本片引入的失败。

## 变异验证（先红后绿）

| 变异 | 被拦住的用例 |
| --- | --- |
| `validate` 去掉占位符检查 | `test_invalid_bundles_are_rejected_with_the_reason[bundle0/bundle1]` |
| `resolve` 改成取最新版而不是钉住版本 | `test_resolve_follows_the_pinned_version_not_the_latest`、`test_readback_after_a_new_publish_keeps_the_original_wording` |

两次变异后恢复源码，19 条全绿。

## CLI 手测（本地临时库）

```
$ aiops-admin consumer-copy publish --tenant T1 --db $T/g.db --file $T/bundle.json
{"tenant_id": "T1", "version": 1, "languages": ["zh"], "published_by": "aiops-admin"}

$ aiops-admin consumer-copy publish --tenant T1 --db $T/g.db --file $T/bad.json   # 丢 {status}
{"published": false, "reason": "zh.status_lede 的占位符必须与内置文案一致：缺 ['status']，多 无"}

$ aiops-admin consumer-copy publish --tenant T1 --db $T/g.db --file $T/bad2.json  # 键名写错
{"published": false, "reason": "zh 没有这个文案键：status_ledee"}
```

## 端到端行为（测试里覆盖，非真实环境）

- 发布 v1 → 提问 → 答复用 v1 措辞；发布 v2 → **回读旧那条仍是 v1 措辞且 200**
  （按钉住的 `copy_version` 复现，不是 503）。
- 把库里正文改成"改过的正文" → 回读被拒绝，改动后的正文不会被交出去。

## 审查发现的两条缺陷与修复（Codex P1/P2）

**P1（回归，严重）：非组合式语种会被英文兜底顶掉。**
`resolve` 对任何语种都返回一个 pack，而 `bundled()` 对未收录语种退回**英文**词表。
于是一个**只发布了中文文案**的租户，德语/法语/繁中的来源标签与整段模板会被换成英文。
修复：`resolve` 对不在 `COMPOSED_LANGUAGES` 的语种返回 `None`（按既有本地化路渲染，
一行都不改）。回归用例 `test_localized_languages_are_never_rendered_from_the_english_fallback`
（de/fr/zh-Hant/th 四语种参数化）；**变异验证**：把这条防线短路，4 条全红。

**P2：格式说明符/嵌套字段能过发布校验，却在渲染时炸。**
`Formatter().parse` 只看字段名，`{status:{missing}}`、`{status!z}`、`{status:invalid}`
都能通过占位符比对，渲染时抛 `KeyError`/`ValueError` —— 等于把"发布时能发现"变成
"用户提问时 500"。修复：发布校验里用假值真跑一遍 `format()`，捕获异常即拒。
三条参数化用例覆盖；变异（去掉这一遍）时三条全红。

## 未完成项（不得读作通过）

- **未完成业务验收**：没有在 41 上发布真实租户文案，也没有真机/原 H5 观感。
- 只覆盖 `zh`／`en`；九种本地化语种仍不可改（`validate` 明确拒绝）。
- 节点路线（`answer_from_run`）仍用内置文案，本片未接。
- 远端 CI、合并、部署与灰度均未开始。
