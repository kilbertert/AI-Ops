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
| `verify_banner_car_lookup.py` | 活口检查：横幅个性化依赖的三条**公司端点**是否真成立（#590） | 改过车图/个性化链路，或要复跑那次验收时 |

## `verify_banner_car_lookup.py` 跑在**网关主机**上，不跑这里

它要两样只在 41 上存在的东西：受试身份来自内网生产库 `ch_my_car`，
凭据在服务自己的 env 里（`AIOPS_MYSQL_*`，0600，root 都读不到）。
端点侧走**公网域名** —— 测的是公司对外真正暴露的那条路径，不是内网绕过。

```bash
scp tools/verify_banner_car_lookup.py aiops-41:/tmp/ && ssh aiops-41 'chmod a+r /tmp/verify_banner_car_lookup.py'
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- bash -c "set -a; . /etc/aiops-41/production.env; set +a; \
  /opt/aiops-41/.venv/bin/python -I /tmp/verify_banner_car_lookup.py --run"'
```

**零写入**：只发 GET，且只碰读端点。写端点（`/add`、`/edit`、`DELETE /{id}`、
`/switchEnableStatus`）一律不试 —— 它们有没有鉴权是公司侧要单独核的事。
退出码 `0` 通过 / `1` 有失败 / `2` **未取证**（默认的 `--plan` 什么都不发；
把"只列了计划"读成"验证通过"正是这个脚本要防的那种假陈述）。
受试身份是真实用户凭据，因此**不打印**车牌、VIN、订单号、用户名与完整 id，
只引用尾号；`--self-check` 是无网络的纯逻辑自检，改坏分桶或判定必转红。

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
