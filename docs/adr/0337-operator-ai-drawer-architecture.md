# 337. 充电桩后台 AI 抽屉后端接口契约：零改码对接公司下载中心、附件只读隔离与 SSE 实时流

状态：已采纳（PRD #1704，覆盖子任务 #1705–#1713 工程实现；真实公司环境/Nacos/模型端到端验收由 #1714 独立承接）。

## 背景

充电桩运营后台新增右侧 AI 排查抽屉前端（`X-Business-Entry: operator`），需在严格遵守「第一版仅允许只读诊断与证据交付」以及「严禁修改公司侧既有仓库代码」的前提下，补齐以下六类后端能力：

1. 首屏智能体声明、后台专属预设问题与 `agent_id` 字段预留（首期固定 `operator_main`）；
2. 会话自动标题、重命名（`PATCH /v1/conversations/{id}`）与完整轮次卡片回读；
3. 图片/PDF/Word（含 `.doc`）多附件上传、纯 Python 有界解析、防提示词注入上下文隔离，以及与公司对象存储/文件中心的对接；
4. 节点执行路径实时投影与最终答复打字机流式推送（SSE + Java 执行节点增量回调 + 复核替换语义）；
5. 结构化引用来源 `citations[]` 与单轮排查报告（`.docx`/`.md`）导出至公司下载中心；
6. 回答点赞/点踩反馈、会话最后一轮原地重新生成，以及 Nginx 显式白名单与公开 OpenAPI 契约更新。

## 决定

1. **零公司代码改造对接公司对象存储与文件中心**：
   - 上传统一调用既有 `cloud-upms` `POST /file/upload`（`from: Y` + `tenant-id`）并固定传 `fileType=doc`，在不改动 `cloud-upms` 代码的前提下自然绕过图片租户水印；同时复用既有 `cloud-mall-admin` `/importRecord/saveImportRecord` 与 `/updateImportRecord`（JSON 序列化严格保持 `exportType` 先于 `direction` 以兼容既有 Jackson 反序列化副作用，沿用 `sussessCount` 字段名）。
   - 附件上传默认登记 `exportType=3101`（落导入中心 `direction=1`），附件下载任务与报告导出默认复用既有 `exportType=10086`（恒落导出中心 `direction=-1`）；Gateway 绝不向前端暴露原始 OSS `storage_url`，仅返回脱敏元信息、缩略图预览端点与 `notify_file_center: true`。
   - 全部参数通过 `GatewayServerSettings` 新增 `AIOPS_GATEWAY_DOWNLOAD_CENTER_*` 受控配置注入，上游异常一律 fail closed（`502 DOWNLOAD_CENTER_UNAVAILABLE` / `400|413 UPSTREAM_FILE_POLICY_REJECTED`）。
2. **纯 Python 有界附件解析与防注入隔离**：
   - 使用 `Pillow`、`pypdf`、`python-docx` 与 `olefile`（解析 Word 97-2003 FIB + Clx Piece Table）在服务端完成格式魔数校验、解压炸弹/XXE 防护、EXIF 剥离、页数/耗时/字符上限截断与加密文件拒绝；无需在容器内安装 LibreOffice 或系统二进制。
   - 附件正文仅封装在受控用户消息 JSON 的 `untrusted_user_attachments` 字段中，并显式附带只读反注入策略声明；附件中的订单号候选仍须经过既有 CPMS 站点/订单授权校验，绝不扩大数据可见范围。
3. **短期 SQLite 事件日志 + SSE 实时流 + Java 节点合批增量回调**：
   - 新增 `assistant_qa_events` 表与 `QAEventStore`，支持 `Last-Event-ID`/`after_seq` 断线续传、15 秒心跳与权限失效立即中断；既有 `GET /v1/assistant/questions/{qa_id}` 同步扩展 `progress.steps[]` 供降级轮询。
   - Java 执行节点新增 `StreamDeltaPusher`，仅在 `operator-agent/1` 的非工具调用最终答复阶段按字符数/时间窗口合批回调 `POST /v1/internal/operator-runs/{run_id}/stream-chunks`，遇到旧 Gateway `404`/超时/5xx 立即静默降级而不阻塞终局 `reportCompleted`。若 Gateway 复核后修改或降级为 `evidence_only`，向 SSE 下发 `message_replace` 整段替换事件。
4. **结构化引用、反馈与最后一轮原地重生成**：
   - `operator_agent.render` 与 `operator_evidence.render_evidence` 保留原 `blocks` 兼容同时产出结构化 `citations[]`；新增 `assistant_feedback` 表支持幂等点赞/点踩/撤销。
   - `POST /v1/assistant/questions/{qa_id}/regenerate` 仅允许会话最后一轮已完成回答原地重生成（递增 `attempt_no`、记录 `parent_qa_id`），复用原问题与已复验附件并在 `completed_context(..., before=turn_no)` 中自然排除旧答复；重生成失败/取消时仅释放占用锁而不删除既有已完成轮次。
5. **显式边界与公开契约**：
   - `ops/nginx/operator-h5-assistant.conf` 与 `deploy/node-runtime/gateway-callback-locations.conf.template` 仅放行显式路径与 HTTP 方法（SSE 独享 `proxy_read_timeout 300s` 与 `Last-Event-ID` 透传，上传独享 `client_max_body_size 25m`），保持管理面与内部回调严格隔离；`public_openapi.py` 与 `docs/testing-api-guide.md` 同步发布 10 条新增用户端点。

## 备选方案

- **在公司 `cloud-upms` / `cloud-mall-admin` 中新增专用导出枚举、绕水印开关与独立预览接口**：可省去 `fileType=doc` 与 `exportType=10086` 适配，但违反「零改码公司侧仓库」硬约束且引入跨仓发版耦合，因此否决。
- **在 Gateway 容器内安装 LibreOffice / antiword 处理 `.doc` 文档并向前端直接返回 OSS 预签名下载链接**：系统二进制增加镜像体积、攻击面与离线可复现构建复杂度，而前端直连 OSS 链接容易绕过租户审计并产生外链泄露风险，因此否决，改用纯 Python `olefile` 有界提取 + 公司下载中心统一分发。
- **直接让前端或浏览器与模型/执行节点建立流式连接，绕过 Gateway 复核**：会绕过 CPMS 授权复核、凭据脱敏与 `evidence_only` 降级门禁，违反本仓安全边界，因此否决，改由执行节点向内部受控端点回调并由 Gateway 经 `message_replace` 兜底收口。
- **允许对历史中间轮次任意重新生成或新增为新轮次**：中间轮次重生成会破坏后续多轮上下文一致性，追加新轮次又会导致会话卡片出现重复提问，因此限定仅最后一轮原地重生成并递增 `attempt_no`。

## 影响与恢复

- 数据库架构版本升级至 `VERSION = 3`（新增 `assistant_files`、`assistant_qa_events`、`assistant_feedback` 三张表及向后兼容扩展列，所有旧表新增列均登记于 `LEGACY_COLUMNS`，可从历史版本原位迁移）。
- 新增运行依赖 `python-multipart`、`pypdf`、`python-docx`、`olefile`，并同步更新 `uv.lock` 与 `deploy/m2-live-03/gateway-requirements.txt`；Java 节点产物同步更新 `deploy/m2-live-03/images.lock.json` 与 `HOST-CHANGE.md`。
- **回滚约束（显式前置条件）**：升级启动后 `gateway_schema.ensure` 会将共享 SQLite 库的 `PRAGMA user_version` 写入 `3`；由于旧版 Gateway 的 `_version()` 对高于 `2` 的架构版本执行 fail-closed 拒绝启动，**上线前必须先对 `gateway.db` 做一致性 SQLite 快照备份（`.backup()`）**。若确需回退至 `VERSION = 2` 的旧版 Gateway，须停机并恢复升级前的 `VERSION = 2` 数据库备份后启动旧版（禁止就地手工改低 `PRAGMA user_version`，以免与旧版表结构校验冲突）。Gateway 新路由位于独立新增端点与兼容扩展字段，不影响现有 C 端与管家 H5 既有接口；Java 执行节点的 `StreamDeltaPusher` 对旧版 Gateway `404` 自动静默降级，仍支持独立回滚节点 JAR。
- 真实公司环境下的 UPMS/Mall 联调、Nacos 上传白名单确认及真实模型业务准确率尚未通过本地测试证明，须通过 #1714 完成真实环境人工验收。
