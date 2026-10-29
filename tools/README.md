# tools/ — 数据生成器与校验器

本目录混合了数据生成器、校验器、隔离实验和现役执行组件。
正式 Java 节点源码位于 `services/execution-node/`，模型代理位于 `services/model-proxy/`；
不能按“工具/旧里程碑”整体删除。归位方案见[目录迁移清单](../docs/architecture/repository-layout.md)。

## 目录内容

| 工具 | 用途 | 何时跑 |
|---|---|---|
| `generate_faq_catalog.py` | 从本地业务 DOCX 生成 `faq_catalog.json` | 内容首次生成 |
| `merge_faq_i18n.py` | 把宽表题面 + 答案 JSON 合并进目录 | 新增语言/内容后 |
| `derive_zh_hant.py` | 由简体权威**派生** `zh-Hant`（opencc `s2twp`） | 改过任何简体文案后 |
| `check_translation_batch.py` | 译文批次的结构校验 | 入库前 |
| `apply_path_map.py` | 把译好的界面路径映射套回答案 | 路径单独译时 |
| `migrate_shortcut_i18n.py` | 生产已发布快捷动作的语言迁移 | 上线新语言后（**有写风险，见下**） |
| `verify_consumer_charging_status_http.py` | A 片自己的 HTTP 面证据：真 `aiops-gateway serve` 在一次性数据根上验证默认关闭、鉴权、拒绝与 no-store；成功读取用同一 app 工厂 + 回环合成授权源 | 改过直读路由的鉴权/缓存/读取行为，或要复跑 A 的 HTTP 证据时 |
| `verify_banner_car_lookup.py` | 活口检查：横幅个性化依赖的三条**公司端点**是否真成立（#590） | 改过车图/个性化链路，或要复跑那次验收时 |
| `check_dify_exposure.py` | Dify 暴露面的段 2/段 3 探测（#583）：真网关上的鉴权/只读语义，以及从 **Dify 自己的容器里**探「登记集合之外一律不可达」 | 改动面向 Dify 的暴露面，或要复跑 #583 的验收时 |
| `dify_registry_blast_radius.py` | 打开运行时注册表之前先量后果（#587）：在**库副本**上用真选择代码对每个 `(租户, 入口)` 各算"开关前/后" | 要给某台主机的清单加 `[[dify_apps]]` 行之前 |


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

## `m2_close/`：隔离实验驱动

下述实验驱动不用于正式运行；节点 jar 从 `services/execution-node/` 构建。
正式部署见[节点运行手册](../deploy/node-runtime/README.md)，两者的生命周期须分别判断。

`run_ic.py` 起一套本机 Docker 网络：MySQL + 编排平台服务端 + 执行节点 + Gateway 代码 +
桩模型 + 桩业务上游，然后跑 IC-01…IC-10。它要两个预置产物（脚本不构建、不下载）：
`SNAIL_SRC`（平台源码 checkout）与 `services/execution-node` 的构建产物。

从**仓库根**执行：

```bash
docker run --rm -v "$PWD":/work -v "$HOME/.m2":/root/.m2 -w /work \
  maven:3.9-eclipse-temurin-21 mvn -f services/execution-node/pom.xml -DskipTests=false package
SNAIL_SRC=/tmp/snail-ai uv run python tools/m2_close/run_ic.py --out /tmp/m2-close.json
uv run python tools/m2_close/verify_real_gateway.py --out /tmp/m2-close-http.json
```

退出码 `0` 通过 / `1` 有判据失败 / `2` **环境不足**（预置产物缺失）/ `3` 实验忙。
两脚本固定共用 `/tmp/aiops-m2close-experiment.lock`，持锁覆盖启动到清理；`M2CLOSE_LOCK`
不生效，不能用不同锁路径同时操作同名容器。退出自动释放文件锁，不手动删除锁文件。报告里逐条列出被替换的
依赖与**未证明**的范围；不要把它读成业务验收。`verify_real_gateway.py` **不**启动平台、
也不执行节点 —— 那部分只在 `run_ic.py` 的容器实验里。

两个容易误读的地方：`--keep` 会把容器留下来便于排查（下一次运行前先删掉，否则会撞名字）；
桩业务上游必须与 Gateway **同容器**，因为 `canonical_gateway_url` 只允许对回环地址用明文 HTTP。

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

## `check_dify_exposure.py`：段 2 在本机，段 3 **在 Dify 的容器里**

段 1（`tests/test_dify_exposure_registry.py`）进 CI，证明"声明的 == 路由表里的"。本脚本补另两半：

```bash
PYTHONPATH=src python tools/check_dify_exposure.py --runtime                        # 段 2：本机真网关
PYTHONPATH=src python tools/check_dify_exposure.py --network --host yidong-36 --host-ip 172.18.0.1  # 段 3
PYTHONPATH=src python tools/check_dify_exposure.py                                  # 只列计划 ⇒ 退出码 2
```

退出码 `0/1/2`（`2` = **未取证**，不是通过），与 `verify_banner_car_lookup.py` 同一纪律。

**段 3 的观察点是容器，不是宿主 —— 这不是细节，是这一段的全部意义。** 判据问的是
"**Dify** 能触达到什么"，而 Dify 是容器：它的 `127.0.0.1` 是它自己，出网还要再经一层
`ssrf_proxy`（squid）。实测（2026-10-09）：同一组内部端口，**宿主上全通**（kb-service 与
RAGFlow 就在那台机器上）、**两个容器里全 `BLOCKED`**。拿宿主当观察点会得出相反的结论。

段 3 还会**自证探针是活的**：先探一个同网络内必然可达的目标（Dify 自己的 redis）。
`/dev/tcp` 是 bash 特性而镜像的 `/bin/sh` 是 dash —— 不显式 `bash -c` 会得到一堆假 `BLOCKED`。
自检不通就退 `2`，绝不把"探针坏了"报成"封锁成立"。

**`api.mall.qushiyun.com` 不在禁止集合里，这是有意的**：它是公司**公网**网关，Dify 的
external knowledge endpoint 就该填它（`deploy/dify-36/README.md` 把这条写成白名单的**正确**填法）。
判据要问的是"那一跳必须落在我们的适配路由上"（路径级，段 1/2），不是"这个域名不可达"。
第一版把它误列进禁止集，跑出两条"REACHABLE ⇒ 失败"—— **错的是登记，不是生产**。
