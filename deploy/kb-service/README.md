# KB 默认模型修复

本目录固定 2026-10-28 读取的 kb-service 基线摘要，保存可重放补丁、构建方法和
现有租户修复脚本。源服务不是 AI-Ops Python 包，不能只改 Gateway 就宣称开户已修。

## 构建与部署

从受控读取取得的 `app.py`、`kb_adapter.py` 所在目录构建：

```bash
python deploy/kb-service/build.py <基线目录> <新产物目录>
python deploy/kb-service/verify_defaults.py <新产物目录>/kb_adapter.py
```

`build.py` 校验输入与输出 SHA-256；基线不匹配就停止。补丁将新建库、克隆缺省和
开户统一为 `text-embedding-3-large@baoyun@OpenAI`，视觉模型为
`gpt-4o-mini@baoyun@OpenAI`；选择默认值时同时匹配厂商和实例，缺模型拒绝假成功。

仅从已合并主线构建部署产物。在 36 用 `dev-host` 读取原文件并复核 manifest 的
`before` 摘要；备份两份原文件到目标声明的 `kb_backups` 下本次专属目录。
用 `dev-host cp 36 ... --artifact-sha256 <摘要>` 送入临时产物目录，原子替换两份文件，
保持原权限与属主；记录主线 SHA 与产物摘要。`systemctl restart kb-service.service`
后核验 `http://127.0.0.1:9380/healthz`。此步骤只重启适配层，不重启 RAGFlow 容器。
失败则原子恢复该次备份两文件并重启适配层。旧服务以 root 运行属于原部署债务，本次不改身份。

## 现有租户

`repair_models.py` 在 RAGFlow 容器工作目录 `/ragflow` 运行。无参数只读检查，
只检查已经显式选了该视觉模型的租户；失败退出 1，不能当作脚本坏了。

```bash
dev-host exec 36 --allow-service-exec --timeout 60 -- \
  'docker exec -i ragflow-kb-ragflow-cpu-1 python3 -' < deploy/kb-service/repair_models.py
```

正式修复从已合并脚本执行 `python3 - --apply --backup <私有新文件路径>`。
脚本先独占创建原默认值备份，再用每个租户已有 API token 调用原生 API，
新增本租户 OpenAI/baoyun 实例与缺失模型，设置默认 embedding/chat/vision，
最后逐项调用真实解析器。密钥仅由该租户原实例现场读取且不输出；不会复制其他租户凭据。
不改已有知识库的 embedding，不重索引它们，也不删除原厂商或模型。
中断后可用新的备份路径重跑；已完成项按原生 API 列表复用。每个租户最后输出 verified。

回滚按备份逐租户恢复原有默认字段，保留新增实例以免误删后来引用；原跨租户视觉默认本来
不可用，因此通常应向前修复。确需回滚时按变更账本指定租户，不能恢复整张表。

API 契约以部署版 `api/apps/restful_apis/{provider,models}_api.py` 与对应 service 为准：
`POST .../instances/{instance}/models` 注册 `image2text`，`PATCH /models/default`
使用 `vision` 与租户自己的 model_id。不要造 UUID 后直接批量写 tenant 表。

## 文档复测

先检查没有进行中的任务，原文与现有切片保留。重新解析只调用
`POST /api/v1/datasets/{kb}/documents/parse`，body 为 `document_ids`；
禁止手写 `run=0` 或清 task 表。轮询到终态，`0.8~0.9` 仍可能正在写索引。
切片分页上限 100，遍历所有页后同时检查 image_id、识别新增文字与图片下载；
`doc_type_kwd=text` 不代表没有关联图片，`image_context_size` 也不是 OCR 开关。

Word 图片按 ZIP 的 `word/_rels/document.xml.rels` 解析；`Target=../NULL` 是
源文件坏引用。恢复副本必须保留正文与 XML 命名空间，凭原图摘要和顺序唯一对应，
不可拿任意图片顶替。原文件保留，恢复副本先验收再切换检索可用状态。

Gateway 图片提示 MIME 依部署版 RAGFlow `image2id` 的 JPEG 编码，实际响应继续由
MediaProxy 字节识别决定，映射 [RFC 9110 §8.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.3)。
父文档扩展名不是图片媒体类型；显式不支持的类型仍拒绝。

## 现场 API 细节与计数修复

2026-10-28 复验：文档 PATCH 的 `enabled` 实际接收整数 `0/1`，布尔值会被拒绝。
停用旧副本用 `PATCH /datasets/{kb}/documents/{old}`、`{"enabled":0}`，原文件和索引保留，
恢复时传 1；确认新文档 DONE、图片和检索通过后才切换。

`parser_config` 的扩展字段放在 `ext`，不能把数据库里的整个 JSON 原样塞回 API。
新上传后只带 chunk_method 的更新可能补回 Raptor/GraphRAG 的默认 true，必须在提交解析前
显式关闭未使用能力并回读：

```json
{"parser_config":{"raptor":{"use_raptor":false},"graphrag":{"use_graphrag":false},"chunk_token_num":512,"ext":{"image_context_size":512,"table_context_size":0,"llm_id":"deepseek-v4-flash@baoyun@OpenAI"}}}
```

旧排查直接清零文档计数而没有减库计数，会让知识库累计值虚增。仅修该目标库的显示计数：
在 RAGFlow 容器 Python 中用 `api.db.db_models` 的 `DB.atomic()` 开事务，对目标
Knowledgebase 和其 Document 行 `select().for_update()`；断言文档集合与已核对集合完全一致、
每条 run=3/progress=1、对应 Task 无 `0<=progress<1`。备份原 `doc_num/chunk_num/token_num`
到独占创建的 0600 文件，然后用 Document 的计数求和更新该库，where 同时比较原值，
断言仅更新 1 行。此处计数包含停用但仍保留的文档，不能把启用数当总数。
不触碰向量数据，不改其他库；有任何并发或集合变化则退出并重新取证。

当前这次对账为总计 210（启用 66 + 停用保留 144）；不再显示历史重复累加的 582。
恢复原计数只会重现虚增，通常不应回滚；如需审计原值，从该次受控备份读取。

原 H5 复验复用 `docs/agents/consumer-wf01/live-release.md` 的正常登录会话与私有隧道。
若提示 `access token validation failed`，记录未通过；不从 Redis 扫描另一个身份替代。
