# 仓库目录规范与迁移记录

## 现行目录规则

本节约束后续开发。下方迁移记录与调查归档解释历史，不能作为新增文件的放置依据。

| 位置 | 应放内容 |
|---|---|
| `src/aiops_diagnostics/consumer/`、`operator/` | C端与管家的业务编排、规则与交付 |
| `identity/` | 公司主体、调用者范围与共享授权规则 |
| `data/`、`health/` | 共享只读数据访问与状态、健康指标/报告 |
| `knowledge/`、`conversation/` | 知识检索/媒体/开户、会话/澄清/固定FAQ |
| `publication/`、`execution/` | 内容发布生命周期（含路由指针推进 `route_advancement.py`）、节点委派合同/运行账本 |
| `onboarding/` | 环境目录登记、草稿/冻结计划、任务状态机、SSH 远程执行器（`*_ssh_runner.py`）、候选试跑与环境正式发布（收敛原横跨 `gateway/`、`dify/`、`publication/`、`consumer/` 的 34 个环境接入模块，保持严格无环分层） |
| `admin/` | `aiops-admin` 运维子命令按域拆分（`onboarding`、`dify`、`knowledge`、`maintenance` 与共用 `common`），根层 `admin_cli.py` 仅做薄 Typer 根应用装配 |
| `dify/`、`gateway/` | Dify协议适配、HTTP装配/配置/调度/存储/观测 |
| `common/` | 已有的跨领域HTTP、文件、配置、语言与生命周期原语；不吸收业务编排 |
| 仓库根 | 已登记的构建、项目指令与交付入口；业务代码、工具和新文档进入对应目录 |
| Python包根 | 版本初始化及三个现役命令实现；新业务模块进入上述职责包 |
| `tests/<职责>/` | 对应职责的行为测试；管家使用`tests/operations/`，避免与Python标准库`operator`冲突 |
| `tests/`根 | 现有跨服务部署、仓库规则、退役检查与共享夹具；新增业务测试进入职责目录 |
| `tools/content/`、`verification/`、`experiments/`、`inventory/` | 内容生成/迁移、验证探针、隔离实验、公司源码索引；tools根只留说明与统一测试命令 |
| `services/`、`integrations/` | 独立运行服务源码（`execution-node/`、`model-proxy/`、`dify-console-bridge/`、`dify-login-proxy/`）、公司侧集成补丁；新增常驻服务不得放进 `src/aiops_diagnostics/` 或 `tools/` |
| `deploy/<服务>/`、`ops/`既有目录 | 成套部署资产、环境清单/代理配置和运行手册；Gateway根层交付脚本保持唯一实现；各部署面归属索引见`deploy/README.md` |
| `llmdoc/` | `@tokenroll/llmdoc` 维护的 agent 检索面（`.mdx` 文档与 `meta.json` 台账）；位置与目录名由该工具契约固定，只能位于仓库根。临时调查材料放本机 `.llmdoc-tmp/`（已 gitignore），不进入检查清单。根层 `.llmdocignore` 同属该工具契约：逐行 minimatch 声明 intentional no-doc 面（过程档案/元数据），匹配路径不再产生 unmapped/dirty 信号 |

未写全前缀的Python目录均位于`src/aiops_diagnostics/`。JSON、HTML、YAML等运行资源跟随使用者所在包，
通过`importlib.resources`加载并验证wheel/服务bundle中的资源；测试夹具放对应测试目录或`tests/fixtures/`。

新增功能先找实际负责该业务的模块。仅为转发增加一层、建立泛化`utils/`收容业务、
把正式运行组件放入实验目录，都不满足本规范。跨包复用依赖现有接口；共享授权规则只保留一份。

### 自动约束与变更方式

`tests/test_repository_layout.py`是根层文件与一级目录的检查清单，覆盖仓库根、`src/`、Python包根、`tests/`、
`tools/`、`services/`、`integrations/`、`deploy/`、`ops/`，并禁止恢复旧`java/`入口。
它检查Git已跟踪及未忽略的新文件，允许在已有职责目录内增加文件；构建缓存不参与判断。
因此新增文件即使被README列出，只要落回根层或新增未登记的一级目录，仍会失败。

现有`test_readme_architecture.py`继续检查职责目录入口、文档链接和新增导入循环，
`test_runtime_retirement.py`继续检查旧模块缺失、退役导入和旧路由不可达。
这三组测试由现有CI全量pytest执行，`verify`是保护主分支的必需检查，无需人工记得另跑。

若确实需要新增一级目录或根层入口，在同一PR中说明职责、调用者和现有目录不合适的原因，
同步本节与检查清单，并按项目ADR规则判断是否需要记录决定。不要仅为消除失败扩大清单。
文件路径可以机械检查，放进已有目录的代码是否职责单一、接口是否合理、资源是否属于该包，仍须审查确认。

本地检查：`uv run pytest tests/test_repository_layout.py tests/test_readme_architecture.py tests/test_runtime_retirement.py`。

`tests/test_ci_test_tier.py`（2026-11-04 新增，根层）守护 `ci.yml` 的测试分档判定：
默认全量，只有改动**全部**落在 `docs/**/*.md` 与根层 `*.md` 时才降级到"读文档的那批"。
它抽 `ci.yml` 里那段判定脚本原文执行，喂进不同改动集合断言算出哪一档 —— 判定写宽
等于把改代码的提交当文档放过，而那种错误看起来完全正常。放在 `tests/` 根层而不是
某个职责目录，是因为它测的是仓库级交付流程（CI），与 `test_cd_deploy_scripts.py`、
`test_repository_layout.py` 同类。

## 2026-10-29：退役验收后开始

Python Thin Harness和诊断/设备CLI已完整删除；独立aiops-admin保留。
C端与管家原H5、Dify原生预览及独立CPMS/跨主体拒绝已通过，证据见
[退役真实验收](../validation/2026-10-29-runtime-retirement-live.md)。历史故障准确率不在此结论内。

第一片将现役Java节点16个文件从`tools/experiments/m2_close/node/`整体迁至`services/execution-node/`，
同步所有现役源码/构建/实验引用；原Java文件逐字不变，宿主安装目录不变。
隔离驱动继续在tools，Gateway镜像不再包含节点源码。验证见
[第一片回执](../validation/2026-10-29-execution-node-layout.md)。

第二片将固定模型出口由`tools/verification/m2_live/model_proxy.py`迁至`services/model-proxy/`，
同步两套Compose、镜像上下文和原测试导入，源码逐字不变；新Compose必须与新路径镜像成对交付。

第三片将C端12个Python模块及语言JSON归到`src/aiops_diagnostics/consumer/`，
23个对应测试归到`tests/consumer/`；导入、打包、monkeypatch和递归静态检查一起更新。
既有节点装配循环按6条具体边记录，本片不改变该业务装配；验收见
[第三片记录](../validation/2026-10-29-consumer-package-layout.md)。

第四片将6个知识模块及9个测试归到`knowledge/`与`tests/knowledge/`；
Dify专用适配、C端知识编排和静态页面入口保留原归属。见
[第四片记录](../validation/2026-10-29-z04-knowledge-package-layout.md)。

第五片将7个身份/范围模块及11个测试归入`identity/`；通用http_auth继续服务独立桥制品。
同一授权逻辑不按端复制，详见[第五片验证](../validation/2026-10-29-z05-identity-package-layout.md)。

第六片将7个发布/内容生命周期模块及7个测试归入`publication/`。
节点执行与Dify协议适配另片处理，见[第六片验证](../validation/2026-10-29-z06-publication-package-layout.md)。

第七片将6个执行委派模块和9个测试归入`execution/`；
现有循环边只更新模块路径，见[第七片验证](../validation/2026-10-29-z07-execution-package-layout.md)。

第八片将25个Dify集成模块和模板归入`dify/`，29测试归`tests/dify/`；
独立桥代码和ExecStart成套切换，见[第八片验证](../validation/2026-10-29-z09-dify-package-layout.md)。

第九片将12个领域模块分入operator/health/data，25测试归operations/health/data，
见[第九片验证](../validation/2026-10-29-z10-domain-package-layout.md)。

2026-10-30追加：14个通用/会话模块与FAQ资源归common/conversation，17测试同迁；
见[本片验证](../validation/2026-10-30-common-conversation-layout.md)。

Gateway追加：10个网关模块归gateway，管家会话归operator，知识HTML跟随knowledge；
22测试归类，三个服务/运维命令保留实际实现。见[验证](../validation/2026-10-30-gateway-package-layout.md)。

工具追加：`tools/`根层按用途分入content、verification、experiments/m2_close、inventory；
公司Java补丁移入integrations/cpms-java，25测试归tests/tooling、9测试归tests/integrations。
见[本片验证](../validation/2026-10-30-tooling-layout.md)。

2026-10-30：上述各片均已合并并部署，源码、测试、工具和公司集成已按职责归类。
包根4文件是版本和3个实际命令实现；tests根保留跨服务部署/退役/仓库规则检查及共享夹具。
部署面保持deploy下的node-runtime、dify-36、dify-chat、kb-service与m2-live-03成套资产；
Gateway交付脚本留在deploy根作为唯一CD入口，ops保留环境清单、反向代理/独立服务资产和运行手册。
这些位置有真实启动/交付调用者，不新增一层转发；移动仓库源码不等于重写现役宿主目录。
最终独立验证与剩余业务边界见[收尾记录](../validation/2026-10-30-z20-layout-final-acceptance.md)。

> 注：退役范围扩大前的 2026-10-28 早期目录调查已按 [ADR-0343](../adr/0343-prune-redundant-drafts-and-obsolete-archives.md) 移出本文件正文，历史原文可在 Git 提交 `57c1d788:docs/architecture/repository-layout.md` 查阅；2026-10-01 公司侧源码探查阶段留下的 6 个历史过程文档取证测试（`#475–#480`）已按 [ADR-0356](../adr/0356-retire-historical-recon-evidence-tests.md) 退役，现役 `deploy/company-gitlab-api.sh` 与探查规程保留；经依赖审计确认现役的 `m2-live-03` / `m3` 工具测试改由 CI `run_deploy_tooling` 按路径门禁（见 [ADR-0357](../adr/0357-converge-p2-test-markers-ci-gating-and-hotspots.md)）。
