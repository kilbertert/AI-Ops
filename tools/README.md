# tools/ — 数据生成器与校验器

这些脚本**不进服务运行时**：它们在开发机上产出或校验仓库内的数据制品，
跑完即可丢弃依赖。服务只读结果。

## 目录内容

| 工具 | 用途 | 何时跑 |
|---|---|---|
| `generate_faq_catalog.py` | 从本地业务 DOCX 生成 `faq_catalog.json` | 内容首次生成 |
| `merge_faq_i18n.py` | 把宽表题面 + 答案 JSON 合并进目录 | 新增语言/内容后 |
| `derive_zh_hant.py` | 由简体权威**派生** `zh-Hant`（opencc `s2twp`） | 改过任何简体文案后 |
| `check_translation_batch.py` | 译文批次的结构校验 | 入库前 |
| `apply_path_map.py` | 把译好的界面路径映射套回答案 | 路径单独译时 |
| `migrate_shortcut_i18n.py` | 生产已发布快捷动作的语言迁移 | 上线新语言后（**有写风险，见下**） |

## 依赖纪律：`opencc` 故意不在 `pyproject.toml` 里

`derive_zh_hant.py` 需要 opencc，但它**刻意不写进依赖清单**：
`deploy/deploy-41.sh` 在依赖清单与生产不一致时**拒绝部署**，而 opencc 是构建期工具、
服务从不 import 它 —— 让它拨动那个生产信号，等于为一个开发期转换器升级生产依赖环境。

```bash
uv pip install --system 'opencc>=1.1,<2'   # 只在需要跑派生的机器上
python3 tools/derive_zh_hant.py --check    # 校验树是否仍是最新派生
```

**代价已写明**：`--check` 进不了 CI（干净检出没有 opencc、也不该有）。
CI 能保证每个 `zh-Hant` 条目**存在**；保证不了它等于其 `zh` 权威的转换结果。

## ⚠️ `migrate_shortcut_i18n.py` 会写，且写的是生产

它经**产品生命周期**（`fork_draft` → `update` → `publish`）改线上行，因此：
改前备份、按动作可回滚、**草稿永不发布**、**停用行跳过并报告**、**绝不覆盖已有值**。
完整步骤见 `docs/agents/env-41-runbook.md` §5.5。

**另一个方向的坑**：`.claude/skills/verify-aiops-client-e2e/scripts/probe_gateway_as_real_user.py`
构造 app 时会调 `recover_interrupted_jobs()`，把在飞作业标 failed —— 那是**验证工具**的写风险，
不是本目录的，但两处都要求"先确认没有在飞作业"。
