# 充电桩后台 AI 抽屉后端上线部署与 41 真实联调回执（2026-11-25）

- **适用版本**：`origin/main` 合并修订 `af3f6bfef98e67fefdb66fee8a4389f9261abb09`（PR #1716，ADR-0337，release 标签 `release-af3f6bfef98e`，覆盖 PRD #1704 与工程子任务 #1705–#1713）。
- **适用范围**：41 宿主机 Java 执行节点 JAR 与回调代理更新、Gateway 运行虚拟环境锁定依赖更新、标准 CD 部署与 SQLite `VERSION=3` 架构升级、下载中心配置与管家 H5 Nginx 入口白名单放行，以及 41 真实环境全链路 API / 附件 / 下载中心 / SSE / 隔离联调（#1714 后端部分）。
- **未触碰边界**：零修改公司侧仓库代码（`cloud-upms`、`qumall`、`cloud-mall-common`、`cloud-charging-pile`、`qumall-admin`），零修改公司业务订单/计费/设备/配置；保持 `business_mutations: disabled`；不触碰 36、95、124 与现役 10 条路由绑定（`chat_route_bindings` 前后逐行相等）。

## 一、已部署组件与制品核对（41 宿主机）

| 组件 / 配置 | 已部署版本 / 制品摘要 | 核对结果 |
|---|---|---|
| 升级前一致性备份 | `/var/backups/aiops-41/pre-af3f6bfef98e-20261125-184743`（`0700`） | SQLite `.backup()` 快照（`0600`、`user_version=2`）+ 两份 env + 节点配置 + Nginx 配置 + 完整 `.venv` |
| 节点回调代理 `aiops-node-callback.service` | `/etc/aiops-node-runtime/gateway-callback.conf`（SHA-256 `0c9523651103b3bd...`，保留宿主机 2 行 Lua 指令） | `nginx -t` 通过，`active`；经节点回连私网 HTTPS 代理对 `POST /v1/internal/execution/runs/{run_id}/stream-chunks` 做 TLS 探活，已放行并直达网关 |
| Java 执行节点 `aiops-runtime-node` | `/opt/aiops-node-runtime/releases/operator-drawer-af3f6bfef98e/node/aiops-execution-node.jar`（SHA-256 `276dac2d851b090cc6bba046af6d58d433d8f4fc7d08c7d32b96c880c06d3d44`，与 `images.lock.json` 逐字节一致）；`AIOPS_NODE_BUILD=af3f6bfef98e-276dac2d851b` | 定点重建仅 `node` 容器（`bde432991f5f`，66s 就绪，gRPC 注册成功）；其余 3 容器 ID（`8fe2344efaa7` / `539aabd0a3b3` / `cb963492a046`）未变；`execution_node_capabilities` 实时心跳正常（`environment='41'`，`operator-agent/1`、`request-target/2`） |
| Gateway Python 依赖 `/opt/aiops-41/.venv` | 离线锁定 wheelhouse（`cp312` `manylinux_2_17_x86_64`，`--no-index --require-hashes --no-deps`）：`lxml==6.1.3`、`olefile==0.47`、`pypdf==7.1.1`、`python-docx==1.2.0`、`python-multipart==0.0.34`；同步合并 `pyproject.toml` / `uv.lock` | `uv pip check` 54/54 兼容；`aiops41` 身份导入验证通过；升级前/CD 后两次 `deploy/check-41-editable.sh` 均 `editable guard passed` |
| Gateway `aiops-gateway-41.service` | 标准 CD run [`45598287463`](https://github.com/kilbertert/AI-Ops/actions/runs/45598287463)（`deploy-41` 41s + `release-notes` 14s）；本次上线基线 `/health` 报 `1.40.4+af3f6bfef98e`（后续 `#1703` `1.40.4+46c200d43ebb` 自动 CD 后亦复核健康） | `PRAGMA user_version = 3`，新增 `assistant_files` / `assistant_qa_events` / `assistant_feedback` 表与扩展列，`gateway_schema.integrity` 通过 |
| 下载中心配置 `/etc/aiops-41/production.env` | 受控内部网络直连公司 UPMS 与文件中心后端服务（具体服务地址留私有环境配置），`upload_export_type=3101`、`download_export_type=10086`（`0600 aiops41:aiops41`） | 以 `aiops41` 身份通过 `GatewayServerSettings.from_env()` 核对 4 项配置全部生效（在 `af3f6bfef98e` 与 `#1703` `46c200d43ebb` 上均复核保留） |
| 宝塔公网 Nginx（管家 H5 与 API 入口） | 更新 `/www/server/panel/vhost/nginx/extension/ulinkmanage.h5.mall.qushiyun.com/aiops-assistant.conf`（SHA-256 `100ed359175a272d...`）与 `/etc/aiops-41/operator-h5-proxy.inc`（SHA-256 `489eefb1664ab35f...`）；全局 `client_max_body_size 50m` | `/www/server/nginx/sbin/nginx -t` 通过并重载生效 |

## 二、41 生产环境真实端到端联调结果（#1714 后端验证）

使用库外加密测试资产 `aiops-ulink-account-20261030`（主租户，按公司管家客户端原 `tenant-app` 密码登录，零验证码识别/绕过）与 `aiops-youlianchong-account-20261030`（第二真实租户，用于跨租户隔离反例），覆盖 `https://ulinkmanage.h5.mall.qushiyun.com`、`https://api.mall.qushiyun.com` 与本地网关受控入口：

| 验证维度 | 验证项与脱敏记录 | 结果 |
|---|---|---|
| 前置配置核对 | 读 Nacos/UPMS 运行态：`spring.servlet.multipart.max-file-size` 150MB/50MB、`max-request-size` 150MB、`file.upload.allowed` 未设拦截；`/file/upload` 与 `/importRecord/{save,update}ImportRecord` 均在线 | 通过 |
| 首屏智能体与预设问题 | `GET /v1/assistant/agents`（三入口均 200，`default_agent_id=operator_main`）、`GET /v1/assistant/starters`（`zh`/`en` 各 4 条对象数组）、未知 `agent_id` 返回 `404 AGENT_NOT_FOUND`；H5 Nginx 拦截 `/v1/internal/...`（405）；`GET /v1/openapi.json` 返回 30 条公开操作无内部泄漏 | 通过 |
| 多格式附件上传与零水印 | 实传合成 `probe_site.jpg`（含 EXIF）、`probe_meter.png`、`probe_log.pdf`、`probe_workorder.docx`、`probe_legacy.doc`（OLE2 Word 97-2003）以及经 H5/API 入口上传 PNG：全部 `201 ready/ready`、响应零 `storage_url` 与零 OSS 链接；伪造魔数 PDF 被拒（400）；`/preview` 经 H5 入口 200 且 EXIF 已剥离；`docx`/`pdf` 手机号已脱敏、`doc` 提取正常；公司 `qumall_mall.import_record` 落库 `direction=1, export_type=3101, state=1, sussess_count=1`；从 OSS 回读 `AI排查附件_probe_site.jpg` 与原图 **SHA-256 逐字节一致（24,698 B），证实 `fileType=doc` 零水印绕过生效** | 通过 |
| 实时 SSE 与 Java 节点增量流 | 结合 `probe_workorder.docx` + `probe_site.jpg` 执行首轮带附件实跑（`agent_runs`: `succeeded / matched / node-41`）：经 H5 Nginx 实时订阅收到 `run_started`、5 组 `step_started`/`step_finished`（含 `parse_attachments`、`authorize_scope`、`dispatch_agent`、`cpms_site_search`、`answer_review`，终态无残留 `running`）、**14 帧真实 Java `message_delta`**、`message_replace`、`citations`（含 `cpms_tool` 与 `attachment` 两类）及 `message_done`；二次续传实跑验证 `Last-Event-ID` 精确断线续传（200，无重无漏）与 `api.mall.qushiyun.com` SSE 转发（200，14 帧）；在途取消实跑返回 `cancelled` 且 SSE 收口 `run_cancelled`（原始 `qa_id` 留私有验收日志） | 通过 |
| 公司下载中心与报告导出 | 依次触发 `POST /v1/assistant/files/{id}/download-task`（1 条）及 `POST /v1/assistant/questions/{qa_id}/export` 的 `.docx` 与 `.md` 导出（各 1 条，共 3 条导出记录，原始记录编号留私有验收日志）：响应均返回 `direction=-1, notify_file_center=true` 且零 `storage_url`；`qumall_mall.import_record` 全部落库 `direction=-1, export_type=10086, state=1, sussess_count=1` 且带有效 HTTPS OSS 地址；回读 `.docx` 可由 `python-docx` 正常打开且含可读中文步骤标题（无 `[running]`），`.md` 含步骤标题、只读免责声明且零原始 URL | 通过 |
| 会话重命名、反馈与原地重生成 | 经 H5 Nginx 执行 `PATCH /v1/conversations/{id}`（200）、`POST /feedback`（`up -> down -> null -> up` 幂等）；2 轮会话内对第 1 轮调用 `/regenerate` 拒绝（`409 REGENERATE_NOT_ALLOWED`），对第 2 轮调用 `/regenerate` 成功（`202, turn_no=2, attempt_no=2, parent_qa_id` 精确匹配）；在 TTL 窗口内经 H5 与 `api.mall` 读 `GET /v1/conversations/{id}` 均 200 返回完整卡片（`turn_count=2` 原地替换，含 `attachments`/`steps`/`citations`/`feedback`） | 通过 |
| 跨租户隔离与既有路由回归 | 使用 `aiops-youlianchong-account-20261030` 跨租户访问主租户附件元信息、缩略图、问答详情、SSE 事件流、会话详情、报告导出与反馈接口，**全部 7 路返回 404**；联调前后 `chat_route_bindings` 10 行（含 5 条 `41` 活跃路由）`revision` 逐行不变 | 通过 |

## 三、现场观察与明确未宣称边界

1. **运行态说明（前端对接需知）**：
   - 管家运行记录（`agent_runs`）保持既有短期回读 TTL（约 3 分钟），超过 TTL 后对旧问答/含超期轮次会话的实时鉴权回读会按原安全策略返回 `410 OPERATOR_RESULT_RETRACTED`（已在超时后实测复现，并非回归）。
   - Nginx `proxy_read_timeout` 限制的是上游两次读取之间的静默间隔而非整条流总时长；网关 SSE 端点在等待新事件期间每 15 秒发送 `ping` 心跳以维持活跃连接。`ulinkmanage.h5.mall.qushiyun.com` 的 `/events` 路由已独立配置 `proxy_read_timeout 300s`，`api.mall.qushiyun.com` 通用 `/v1/` 路由保持 `proxy_read_timeout 120s`（本次实测 14 帧 SSE 与短耗时排查均正常返回；若上游发生超过 120 秒无任何事件/心跳输出的极端阻塞且经 `api.mall` 域名接入，可按需为该域名单独拆出 `/events` 路由并上调静默读超时）。
2. **本回执明确不宣称的内容（#1714 保留人工验收项）**：
   - 尚未执行前端抽屉 UI 落地后的浏览器端联调（包括界面打字机渲染、调用公司前端既有 `notifyAndOpenFileCenter(-1)` 触发「导出中心」弹窗、以及人工在文件中心 UI 列表点击下载）；
   - 未经工程师确认真实线上故障根因，**不宣称业务诊断准确率已验收**；
   - 同租户普通员工（非主账号 `organ` 站点范围变更撤权）的真实线上复核与大文件 20MB 压测仍待前端联调窗口补充。
