# 捷佳四轮国内充电（`jiejia`）V2.1 纯前端零命令行重开全链路验收回执（2026-11-27）

- **适用版本与运行面**：
  - `41` 控制面 Gateway & 管理编排：**`1.43.17+0827836d4d1e`**（提交 `0827836d4d1e501f3fe354c7781efffb2ddf8057`，标准 CD run [`45932668739`](https://github.com/kilbertert/AI-Ops/actions/runs/45932668739)）；中央生效目录推进至 **R29**（`41` `2/2` 启用、`jiejia` `1/1` 启用、`95` `0/2` 停用，三环境稳定共存）；专属共享执行节点容器 `aiops-environment-jiejia-environment-node-1`（`aiops-environment-node@jiejia.service` `active`，`restarts=0`），Snail 平台登记 `id=5`（`appId=aiops-environment-jiejia`, `onlineNodes=1`）。
  - `jiejia` 客户机目标接入模块（逻辑主机 `jiejia-gw`，角色 `service-host`）：由控制面离线制品池经浏览器 Step 3 自动离线冷装并滚动升级至 **`1.43.16+c98ac2831b9b`**（提交 `c98ac2831b9b155d02fee10943a96b39002ae8b1`，`http://127.0.0.1:8788/health` 返回 `ok: true`、`business_mutations: disabled`）；`jiejia` `environment.json` `revision=1`、`enabled: true`，唯一租户 `1990990768688599040`（捷佳）**正式启用在线**。
  - `36` 共享 Dify 控制台与知识服务：`location = /__aiops/manage` 已随 PR `#1793` 将 `proxy_read_timeout` 对齐为 `180s` 并完成 `nginx -t && nginx -s reload`；为 `jiejia:1990990768688599040` 配齐独立工作区 `c8c0a12f-7e0c-4564-8f40-cb1bc212bca0`、对客隔离知识库 `fe180a36e97b11f1a026f1f706dc4043`、管家隔离运维知识库 `89321e06e97f11f1a026f1f706dc4043`、对客应用 `47e64e25-1588-5f4f-b350-97e37963fcc1`（workflow `dac852fe-dffb-4a1c-88d6-a04e1c279d50`）与管家应用 `71a466cc-7298-58ef-bad2-7f6031239e8e`。
- **适用范围**：
  1. 完整覆盖「阶段 A（V2.1 六大断裂实机执行器与真机问题闭环 PR `#1781`–`#1793`） → 阶段 B1（无损清理既有 `jiejia` AI-Ops 资产至未开通初态） → 阶段 B2（纯浏览器访问 `https://dify.ranlei.work/aiops/environments` 完成 5 步零命令行重开） → 阶段 B3（公网终验、无明文密钥审计、临时账号与文件清理）」；
  2. 验证 V2.1 自助向导可仅凭客户基础信息与浏览器图形验证码交互，独立完成主机纳管、数据面预检、基建编排与冷装/离线升级、向导内知识上传解析、并发免 JSON 三能力自验、正式发布激活及公网 4 项 HTTPS 终验。
- **未触碰与诚实边界**：
  - 零客户业务代码改动、零客户业务表写操作（Gateway `business_mutations: disabled`）；原始主机网络地址、密码与密钥引用保存在受控运维记录中，绝不入库或回显。
  - **`business_accuracy="unconfirmed"`**：本回执证明 V2.1 纯前端五步零命令行开通、三能力真机取数及公网正式 HTTPS 通路可用；人工故障诊断语义准确率仍待工程师/业务方签收。

---

## 一、纯浏览器 5 步零命令行开通实测链路

| 向导步骤 | 页面操作与后台执行器 | 实测结果与关键证据 |
|---|---|---|
| Step 1 主机接入 | 页面录入客户基础信息并完成一次性引导建钥 | 客户机专用只读接入身份、控制面钉扎 SSH 密钥与 `0600` 运行模板建立 |
| Step 2 体检与修复 | 自动只读探测目标 MySQL / Redis / TSData 与 UPMS 租户 | 9 项体检全绿，自动发现并绑定唯一业务租户 `1990990768688599040`（捷佳） |
| Step 3 基建与部署 | 点击「一键配套周边基建并探活」（`infra_provision`） | 控制面离线制品池（12 条目）自动匹配并完成 5 子步：`allowlist`、`execution_node`、`cold_install_and_onboard`、`customer_nginx`、`live_probe`（`匿名=401, 原路由=200`）全部 `passed`；`chat_route_bindings` 升级为 `operator` `v2 (rev 2)` 与 `consumer` `v2 (rev 2)` |
| Step 4 知识与自验 | 向导内置上传双隔离知识库；人工于 Chrome Tab 2 过图形验证码获取 `1990990768688599040` 租户令牌并点击「一键执行三能力自验（免写 JSON）」 | 对客库装入通用试跑模板 `aiops-consumer-starter-faq.md`（1 切片，引用 `4abb5cd7f1fb5b78`）；管家库上传 `AI充电桩运维系统_第一期周期可用报告_详细研发方案.docx`（14 切片）。并发自验 **HTTP 200（耗时 23.8s）** `passed: true`：`consumer`（`qa_287e8b5a91f14b559b27f82f8a0dd1c4`）、`order`（`qa_eff938f7f9544e4d8ed9171cb083c61c`，样本订单 `2126030234563444737`）、`offline`（`qa_5aaee74618214c6b9e43e325460b676c`，样本设备 `2124403774106959873`）全部 `passed`；`credentials_persisted: false`、`operator_session_revoked: true`、`business_accuracy: "unconfirmed"` |
| Step 5 发布与终验 | 点击「一键正式发布并启用租户」（`publish_environment`）及「执行公网正式 HTTPS 4 项终验」（`post_publish_smoke`） | 发布任务 `wizjob_c359be87a05aa149f6e868c0` 全部 3 子步（`enable_registry`、`sync_target_runtime`、`activate_node_route`）`passed`，中央目录 **R29**、`published: true`；公网终验 `passed: true`（`public-https-anon-rejected: 401`、`public-https-customer-route-intact: 200`）并渲染交付回执卡片 |

---

## 二、发布后独立公网复核与共存状态

从 `41` 向客户正式公网域名 `https://api.juxichongdian.cn` 独立发起的发布后核验全部通过：

1. **匿名保护**：`POST /v1/assistant/questions`（无凭证）返回 `HTTP 401`；
2. **既有客户路由回归**：`GET /upms/tenant/list` 返回 `HTTP 200`；
3. **发布后 C 端正式 HTTPS 问答**：以授权 C 端测试会话带 `client-type: H5` 发起真实问答（`POST 202 → GET 200 completed`），生成 `qa_1858659c26074c2aa73bdb23a651e64d`，`error=null`，返回 3 段完整结构化块；
4. **多环境共存目录核对（R29）**：`41` 启用（`2/2` 租户启用）、`95` 保持停用（`0/2` 租户启用）、`jiejia` 启用（`1/1` 租户 `1990990768688599040` 启用），`41` 与 `jiejia` 健康检查均为 `ok: true`、`business_mutations: disabled`。

---

## 三、端到端实机暴露并即时闭环的工程问题（`v1.43.10`–`v1.43.17`）

本次从全新未开通态重走纯浏览器链路期间，实机捕获并全部合并 CD 上线的问题如下：

| 问题现象 | 根因定位 | 修复 PR 与版本 |
|---|---|---|
| C 端扩展检索被客户 Nginx 403 拦截 | `wizard_worker` 白名单写入历史固定出口 IP，未同步当前 `36`/`41` 实际出口 | PR `#1785`（`v1.43.10`） |
| 向导免 JSON 换取 C 端 `thirdSession` 报系统错误 | `/mallapi/userinfo/phone/login` 缺少请求头 `client-type: H5` | PR `#1786`（`v1.43.11`） |
| `node-jiejia` 启动报 `Unable to access jarfile` 及 C 端 `content_snapshot` 校验失败 | 节点 jar 文件名及目录属主权限未对齐容器 UID 999；消费侧快照缺少 `schema: aiops-consumer-live/1` | PR `#1787`（`v1.43.12`） |
| `node-jiejia` 启动报清单路径不可解析 | `node.env` 的 `AIOPS_EXEC_MANIFEST` 写入宿主机路径而非容器挂载 `/etc/m2live/exec-manifest.json` | PR `#1788`（`v1.43.13`） |
| `node-jiejia` 心跳被 Snail 拒绝 | Snail `/app/page` 密钥字段解析、`/agent` 字段及 `/openapi/v1/user/*` 注册订阅契约与权威脚本不一致 | PR `#1789`（`v1.43.14`） |
| 客户机已有 `installed.json` 时重跑 Step 3 未升级 venv | `_step_cold_install_and_onboard` 仅判定文件存在即跳过 | PR `#1790`（`v1.43.15`） |
| Step 4 管家链路报 `ExecConfigViolation: OPTIONS` | `_step_execution_node` 硬编码 `'model_options': {}`，与 Snail 模型 `configJson`（`temperature=0.3, max_tokens=4096`）漂移；`seed_target_gateway_db` 缺既有 `operator` 清单变更升级分支 | PR `#1791` / `#1792`（`v1.43.16`） |
| 3 能力自验后台全部完成但浏览器 60s 报 `504` | `run_live_self_validation` 串行耗时约 72s，且 `36` Nginx `/__aiops/manage` `proxy_read_timeout 60s` 低于 `console-bridge` 的 `180s` | PR `#1793`（`v1.43.17`） |

> 注：调试期间曾因超管账号 `system` 所持 JWT `tenant_id="-1"` 被 `jiejia` 单租户 `RegisteredCallerResolver` 正确拒访（`POST_REJECTED`）；改用租户 `1990990768688599040` 真实会话后即按预期放行并完成自验，随后通过自动吊销与清理确认无权限残留。

---

## 四、无明文密钥审计与临时痕迹清理

1. **临时验收账号与文件清理**：在客户 UPMS 删除本次创建的临时账号 `aiops_verify_363777`（`2126152111344943105`）与 `aiops_verify_20261127`（`2126131767829692417`），确认历史临时账号 `aiops_verify_20261126`（`2125895430065192962`）保持 `lock_flag='9'` 锁定态；删除云电脑临时 `.docx` 文件并关闭验收标签页。
2. **全链路零明文泄露非打印审计（7/7 `clean: true`）**：以仅状态码匹配的方式校验 `config-jiejia.env` 全部敏感字段（数据库口令、Redis 口令、内部签名密钥、`third_session` 服务令牌）在以下目标均 0 命中：
   - `41`：`registry.json`、`customer-hosts.json`、`registry.json.drafts.db`、`journalctl -u aiops-gateway-41 -n 500`；
   - `dev-host`：`journalctl --user -u aiops-site-preflight.service -n 500`；
   - `36`：`journalctl -u aiops-dify-console-bridge` / `aiops-dify-login-proxy` 近 300 行；
   - `jiejia`：`journalctl -u aiops-access-jiejia.service -n 500`。
3. **受控文件权限核查**：`41` `/var/lib/aiops-41/environment-registry/wizard-templates/jiejia.env`（`0600`）、`jiejia` `/etc/aiops-access-jiejia/production.env` 与 `registry/environment.json`（`0600`）、`36` `/etc/aiops-dify-api/allowed-sources.conf`（`0644`）。
4. **后续跟进项**：
   - 待客户或内部工程师人工抽测真实故障案例如实签收 `business_accuracy`；
   - 建议客户将对客知识库内置临时试跑 FAQ 替换为正式官方客服文档，并按运维规范轮换交付阶段使用过的服务器密码。
