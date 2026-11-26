# 捷佳四轮国内充电（`jiejia`）真实客户环境端到端开通与正式 HTTPS 验收回执（2026-11-26）

- **适用版本与运行面**：
  - `jiejia` 目标业务环境（受管逻辑主机 `jiejia-gw`，角色 `service-host`，`.dev-host.toml` 经 PR `#1751` `3877f323` 声明）：经 `/aiops/environments` 真实浏览器 ADR-0308 冷安装后，随 PR `#1756`（`e82e734d`）经受管安装器升级至 **`1.41.0+e82e734d68ff`**（`http://127.0.0.1:8788/health` 返回 `ok: true`、`business_mutations: disabled`），仅运行最小接入模块，唯一活跃租户 `1990990768688599040`（捷佳）**正式启用在线**。
  - `41` 控制面与共享执行面：Gateway 保持 **`1.41.0+688e751e93f6`**（`business_mutations: disabled`）；中央生效目录推进至 **R18**（`4 个环境 · 3/7 租户已启用`：`41` `2/2` 启用、`jiejia` `1/1` 启用、`95` `0/2` 停用、`124` `draft_only`）；共享执行节点 `node-jiejia`（`aiops-environment-node@jiejia.service`，零宿主机暴露端口）在线心跳正常。
  - `36` 共享 Dify 控制台：`deploy-web-1`（`onboarding-6dac04a1`）、`kb-service --scoped-account`、多环境登录代理与管理桥保持运行；为 `jiejia:1990990768688599040` 独立分配工作区 `c8c0a12f-7e0c-4564-8f40-cb1bc212bca0`、对客知识库 `fe180a36e97b11f1a026f1f706dc4043`（连接 `ce6b5047-0258-45a6-945a-a9c49370bc03`）、隔离的管家端运维知识库 `89321e06e97f11f1a026f1f706dc4043`、对客应用 `ad90e791-92c6-5a2c-8d26-428ec93194e4` 与管家应用 `d76a5bd5-5ed2-567e-ba3b-b80ad18e7dd5`。
- **适用范围**：
  1. 首个真实客户环境从受管主机准入、共享执行节点与 Baota Nginx 双扩展安装，到通过 Dify `/aiops/environments` 真实浏览器驱动完成 ADR-0308 冷安装、受管升级、三能力业务技术验证与正式发布激活的全流程实测；
  2. 客户公网正式域名 `https://api.juxichongdian.cn/v1/assistant/questions` 的匿名拒访、客户原路由回归、C 端（`third-session`）与管家端（`Bearer` + `X-Business-Entry: operator`）端到端正式问答核验；
  3. 与现有 `41`（`2/2` 启用）和 `95`（`0/2` 停用）共存无干扰核验，以及临时验收账号与用例文件的闭环清理。
- **未触碰与诚实边界**：
  - 零客户业务代码改造、零订单/账务/设备写操作（Gateway `business_mutations: disabled`）；具体主机地址、凭据与脱敏外原始记录保存在私有运维目录，不入公开文档。
  - **`business_accuracy="unconfirmed"`**：本回执证明的是真实客户环境一键开通编排、工具取数与公网正式 HTTPS 问答通路可用，**不宣称**已由客户工程师或业务方完成语义准确率人工签收。

---

## 一、浏览器驱动冷安装、受管升级与三能力验证

1. **前置配套与 ADR-0308 冷安装**：
   - 在 `jiejia-gw` 挂入工具扩展与正式用户入口两份 Nginx 扩展（`nginx -t && nginx -s reload` 通过），在 `41` 启动专属执行节点 `node-jiejia`（`aiops-environment-node@jiejia.service`）。
   - 在真实浏览器 `/aiops/environments` 驱动 `tenant-onboard` 任务 `task_90fa7ecd0037b58f7e585cdb271c56a9`，11 个子阶段（`site-install` → `site-preflight` → `workspace-prepare` → `knowledge-prepare` → `knowledge-connect` → `operator-prepare` → `consumer-prepare` → `candidate-prepare` → `candidate-install` → `candidate-register` 等）全部收敛至 `observed`。
   - 分别向对客知识库上传验收 FAQ `jiejia-consumer-faq-validation-20261126.md`（文档 `893ccdc4e97f11f1a026f1f706dc4043`，切片 `10842dd4fa92c98f`），向独立管家知识库上传负责人指定的《AI充电桩运维系统_第一期周期可用报告_详细研发方案.docx》（文档 `896f2e40e97f11f1a026f1f706dc4043`，14 切片，检索命中分数 `0.8923`）。
2. **时序库可选列兼容修复与受管升级**：
   - 实测暴露客户 TDengine `charging-gun_property` 缺少可选列 `errorCode`/`errorReason`/`meterNow`，通过 PR `#1756`（`e82e734d`）实现遇到 `Invalid column name` 时单次回退全列查询并按 `shape_keys` 严格投影。
   - 在 `41` 注册 `1.41.0+e82e734d68ff` 离线构建制品，在浏览器 `/aiops/environments` 触发受管升级（`task_6c365232ba6c4746acb2bfa78684751a` / `candidate-install` `task_e07b8f9cf065d48c2a77e75dce53a5b1`），验证 `jiejia` `/health` 升至 `1.41.0+e82e734d68ff`。
3. **三能力业务技术验证全绿（`task_eda29bb1ae5e3834d034e28d134684a2` / `plan_7fcf080d0025773fcd8be37675e66ddf`）**：

| 能力项 | 验证状态 | 运行记录 ID | 命中工具与对象证据 |
|---|---|---|---|
| `consumer`（客服知识与本人订单） | `passed` | `tr_5af6a202c18f40c7b98bb0a6d6607597` | `tools=["knowledge","status"]`，命中知识切片 `10842dd4fa92c98f` 与测试订单 `2092158656121733121` |
| `offline`（设备离线诊断） | `passed` | `qa_9dc6a7682c1b480781744d41ef3d7555` | `tools=["cpms_device_offline"]`，设备 `2121870666501521409`，节点 `node-jiejia` |
| `order`（订单异常与过程统计） | `passed` | `qa_459041696a98464bb471db19edbe285f` | `tools=["cpms_charging_timeline","cpms_order_detail","cpms_order_process_stats"]`，订单 `2125879212020924417`，节点 `node-jiejia` |

4. **正式发布与激活（`task_e739fea4ea1c377913bc041068466c5b`）**：
   - 在浏览器确认正式发布，任务收敛为 `state=observed`、`phase=activated`、`activated=true`、`directory=synchronized`（中央目录 **R18**，`routes={"operator": 1, "consumer": 1}`）。
   - 发布版本：管家端 `agt_d57536a80bea419fafa9976b7df6ec8d`（`v1`），对客端 `agt_20273dad6131433dba7509f996a400a1`（`v1`，workflow `8d57d206-1f35-41fc-9c56-d76218eed413`）。

---

## 二、公网正式 HTTPS 入口验收（`https://api.juxichongdian.cn`）

| 核验项 | 请求形态 | 实测结果 | 结论 |
|---|---|---|---|
| 匿名拒访 | `POST /v1/assistant/questions`（无凭证） | `HTTP 401`（`INVALID_ACCESS_TOKEN`） | 通过 |
| 客户既有业务回归 | `GET /upms/tenant/list` | `HTTP 200`，正常返回捷佳租户 `1990990768688599040` | 通过 |
| C 端正式问答（默认及显式 `X-Business-Entry: consumer`） | `third-session` + `tenant-id` 提交并轮询 | `HTTP 202 → 200 completed`（`qa_e0174e9e9fbc40a39712725729cc8619`、`qa_27380b4074bf44e083629a1330674006`），返回对客知识引用 `10842dd4fa92c98f`（`retrieval_status: found`）与订单 `2092158656121733121` 状态 | 通过 |
| 管家端正式问答 | `Authorization: Bearer` + `X-Business-Entry: operator` 提交并轮询 | `HTTP 202 → 200 completed`（`qa_1d6b3c5b19eb41098fce046ec7be7edb`），通过 `node-jiejia` 完成 4 项工具取数（订单详情、时序遥测、监测样本、过程统计）并诚实标注缺证 | 通过 |
| 现役环境共存核验 | `41` 与 `jiejia` 健康检查及管理页总览 | `41` Gateway `1.41.0+688e751e93f6`（`2/2` 启用）与 `aiops-environment-node@jiejia.service` 均 `active`；`95` 保持 `0/2` 停用；总览 `4 个环境 · 3/7 租户已启用 · 目录 R18` | 通过 |

---

## 三、真实客户首装暴露的问题与现场处置记录

1. **宿主机冷装前置依赖**：目标主机默认无锁定非登录系统账号 `aiops` 及 `python3.12-venv`（`ensurepip`），按服务账号隔离规范补建系统用户 `aiops` 并安装 `python3.12-venv` 后冷装一次通过。
2. **时序服务路由差异**：客户业务网关 `/tsdata/**` 转发目标端口未监听，实际 `cloud-tsdata-biz` 监听于本机回环独立端口，通过受管配置切换 `AIOPS_GATEWAY_OPERATOR_TSDATA_URL` 直连本机回环时序服务解决。
3. **TDengine 表结构可选列差异**：客户时序表 `charging-gun_property` 缺少 `errorCode`/`errorReason`/`meterNow`，已通过 PR `#1756` 通用化修复并实机升级验证。
4. **设备离线监测索引缺失**：经负责人明确授权，在客户 MySQL 执行在线无锁 DDL（`ALGORITHM=INPLACE, LOCK=NONE`）为 `cloud_charging_pile.iot_device_offline_monitor` 新增组合索引 `aiops_offline_device_time (device_id, offline_time)`，`aiops_ro` 只读 `EXPLAIN` 验证命中 `type=range, rows=1`。
5. **只读数据库账号权限基线补充**：首轮公网正式 `POST /v1/assistant/questions` 暴露 `503 PLATFORM_UNAVAILABLE`，根因为 `MySQLPlatformDirectory` 需只读解析 UPMS 平台角色而 `aiops_ro` 仅获授 `cloud_charging_pile.*` 与 `qumall_mall.*`。经负责人授权补授 `qumall_upms.{sys_user,sys_user_role,sys_role}` 3 张表最小只读 `SELECT` 权限后，双端正式入口全部通过。
6. **临时验收痕迹清理**：验收通过后，已在 `jiejia` UPMS 锁定临时管家测试账号 `aiops_verify_20261126`（ID `2125895430065192962`，`lock_flag='9'`）并注销会话，清除 `jiejia` `/etc/aiops-access-jiejia/validation-cases/` 下临时用例与会话文件，清除云电脑临时令牌并挂起云电脑实例。

---

## 四、后续待跟进项

- **业务准确率签收**：`business_accuracy="unconfirmed"`，待客户或内部运维工程师结合真实故障案例人工复核。
- **对客知识库正式资料替换**：当前对客库含 1 份临时验收 FAQ（`jiejia-consumer-faq-validation-20261126.md`），待客户上传正式对客文档后按需替换清理。
- **凭据轮换**：建议客户运维在验收完成后轮换交付过程中以明文提供的主机 `root` 密码及后台 `system` 账号密码。
