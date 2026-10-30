# 子任务与依赖顺序

每个子任务都是一个可独立提交/合并的逻辑单元。`blockedBy` 是**硬依赖**：前一项没有可运行结果时，
后一项不做。批次对应任务书 §3 的 ①②③④。

| ID | 子任务 | 批次 | blockedBy | 交付物 | 验收编号 |
| --- | --- | --- | --- | --- | --- |
| T1 | 实施规格与验收合同入库 | 0 | — | 本目录 `implementation-spec.md` / `acceptance.feature` / `qa-plan.md` / `tasks.md` | — |
| T2 | 受管应用目录区分端/场景（`operator_apps` 加 `entry`/`scenario`/`targets_json`，`SelfService.managed` 双端可解析） | 1 | T1 | `dify/self_service_store.py`、`dify/dual_entry.py`、`dify/self_service.py` + 测试 | A、K1 |
| T3 | 目录 API 双端来源与统一进度（`catalogue.entries`、`operations[].scope/entries`） | 1 | T2 | `dify/self_service_api.py` + 测试 | A、B、J2、K1 |
| T4 | 开通任务按端分派（`scope`、逐端步骤与状态、部分完成、重试续跑） | 1 | T3 | `dify/self_service.py`、`self_service_store.py` + 测试 | A、B、C1、D |
| T5 | 客户端应用自动准备：复制来源 DSL 并重绑知识引用（拒绝源客户库UUID） | 2 | T4 | `dify/self_service_prepare.py`、`dify/dify_console_admin.py` + 测试 | B、F、G3 |
| T6 | 服务凭据自动申请与 0600 落盘（先快照后核对，丢回执对账） | 2 | T5 | `dify/service_keys.py`、`dify/dify_console_admin.py` + 测试 | C3、L1 |
| T7 | 受管客户端发布目标替代逐客户文件登记（受管行第一权威 + 冲突显式拒绝） | 2 | T5 | `consumer/consumer_publication.py` + 测试 | G1、G3、L1 |
| T8 | 客户端草稿预览绑定当前受管草稿（不要求正式版、不推进指针） | 3 | T7 | `consumer/consumer_trial.py` + 测试 | E1 |
| T9 | 双端逐端发布/撤回/恢复（CAS、不回落、不跨端） | 3 | T8 | `dify/self_service.py` + 测试 | E2–E5、K2、K3 |
| T10 | 渠道与就绪口径分开（资源/已发布/渠道/真实用户） | 3 | T9 | `dify/dual_entry.py`、`dify/self_service.py`、页面 + 测试 | J1、J2 |
| T11 | 前端双端入口与逐端进度（Dify web patch 组件） | 3 | T10 | `deploy/dify-36/web-patches/1.17.1/aiops-management.tsx` + browser spec | A、B、J2 |
| T12 | 现役资源纳管与配套部署（受管行迁移、key 目录、逐组件核对、恢复点） | 4 | T11 | 部署记录 + `docs/agents/` 手册更新 | L1、L2 |
| T13 | 真实双端验收（真实消费者与员工账号、原页面、QA/run/版本关联） | 4 | T12 | 私有证据 + 本目录记录 | H、I、J1、L |

## 顺序说明

- T2 先于一切：目录不能区分端，后面的"各选来源"没有落点。
- T4 先于 T5：客户端准备挂在"按端分派"的步骤记录上，否则失败无法逐端续跑。
- T6 独立于 T7 但必须先于 T9：没有可读凭据的发布等于"发布了也跑不起来"。
- T8 先于 T9：草稿预览是"保存不发布"的可验证形态，先有它才能证明发布确实推进了指针。
- T12/T13 是真实环境动作，前置全部代码合并并部署；缺条件时记录剩余条件，不降标准。

## 不在本清单

新域名/小程序/安装包、公网暴露、账号提权、额外主机、模板批量升级、跨环境搬迁、
多实例统一凭据服务、任何业务写动作。这些需要单独设计与审批。
