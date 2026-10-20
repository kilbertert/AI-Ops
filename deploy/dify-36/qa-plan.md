# 36 上 Dify 栈：向量库 / 检索端点 / 库卫生的系统验收计划

对应 `acceptance.feature`。每一项都写明**环境**、**前置**、**操作**、**可观察结果**与**清理**；
「在控制台里试过」不算证据，必须留下 HTTP 状态码、返回体片段或 SQL 结果。

## 记录要求

每项记录：时间、制品身份（`deploy/ARTIFACT.sha256` 或 `patches/apply.sh` 输出）、环境、
步骤、状态码/结果行。**不记录任何凭据**：设备/共享密钥只写「凭据来源」，不写值。
未执行的项目标「未完成」，不得用 fixture 或单元测试顶替。

## 用例

| ID | 环境与前置 | 操作 | 预期可见结果 | 清理 |
|---|---|---|---|---|
| VEC-01 | 36；`WEAVIATE_API_KEY` 与 `VECTOR_STORE` 已配；weaviate 就绪 | `docker compose up -d` 后 `GET /v1/.well-known/ready` | 200；`deploy-api-1` 的 env 里 `VECTOR_STORE=weaviate`、`WEAVIATE_ENDPOINT=http://weaviate:8080` | 无 |
| VEC-02 | 36；vendor 数据集 `云快充平台协议V1.6.pdf…`（document `f642ab5c…`，1564 段） | 控制台点「重新索引」或 `POST /console/api/datasets/<id>/retry` | `documents.indexing_status` 变为 `completed`；`select count(*) from embeddings` > 0；weaviate collection `Vector_index_244617c2_…_Node` 有对象 | 无（这是要修好的数据） |
| VEC-03 | 隔离 compose（不含 `VECTOR_STORE`） | 起栈并上传一份文档 | 文档 `error` = `Vector store must be specified.`；这一项**故意失败**，用来证明 VEC-02 的红/绿不是偶然 | 销毁隔离栈 |
| RET-01 | 36；external 数据集 `aiops-41-kb-canary-debug`（绑定 `kb-canary`） | 用一把 **dataset 类型** API key 调 `POST /v1/datasets/<id>/retrieve {"query":"…"}` | 200；`records[].content` 非空、`metadata` 为对象；**没有** `segment` 字段 | 删除该临时 key |
| RET-02 | 同上，但把 API 模板的 endpoint 改成一个必然 502 的地址 | 调同一端点 | 非 2xx（服务端已知的 external 失败路径），**不是** `records: []` 的成功响应 | 恢复 endpoint |
| RET-03 | 36；vendor 数据集（VEC-02 完成后） | 调同一端点 | 200；`records[].segment` 结构仍在（vendor 形状未被补丁改变） | 无 |
| SEC-01 | 36；控制台 owner 会话 | 打开「外部知识库 API」编辑框并保存（不改密钥） | 保存成功；DB 里 `settings` 不含明文 `dify-` 前缀的值 | 无 |
| SEC-02 | 36 | `select settings from external_knowledge_apis;` | `api_key` 形如 `enc:v1:<base64>`；明文只出现在控制台响应里 | 无 |
| SEC-03 | 36 | 触发一次 external 检索（RET-01 即可） | 网关访问日志显示 `Authorization: Bearer dify-…`（即密文已被正确解密后才发出） | 无 |
| BIND-01 | 36；先造一个一次性 external 数据集并绑定 `kb-canary` | 通过控制台删除该数据集 | `external_knowledge_bindings` 中该 `dataset_id` 的行为 0；API 模板的 `dataset_bindings` 不再列出它 | 无需（数据集已删） |
| BIND-02 | 36；现存的 4 条孤儿绑定（`f5fce915…`/`0be8c6e0…`/`1571ad21…`/`ec157ddb…`/`13e58e09…`/`a08bd9f7…` 中仍无 dataset 的） | 逐条确认目标数据集不存在后删除 | `external_knowledge_bindings` 与 `datasets` 的 left join 不再产生空 `name` 行 | 无；删除前把行导出为证据 |

## 部署与回滚

- 制品：`patches/apply.sh` 在 36 上用**同一镜像**重放补丁并核对 5 个文件的 sha256；
  摘要不匹配即中止（镜像升级后必须先重做补丁）。
- 回滚：把 `deploy/dify-36` 恢复到上一版并 `docker compose up -d`。weaviate 容器与
  `./volumes/weaviate` 可以留着（没有 `VECTOR_STORE` 时它不被使用），也可以
  `docker compose stop weaviate`。
- **明文密钥的轮换是独立一步**：加密只让新写入与迁移后的行不再是明文；历史上已落过盘的
  值必须视为已泄露，在 41 与 Dify 两侧同时换新值，旧值作废。换值与迁移的顺序：
  先换签名侧（41 接受新旧两把的窗口最短），再改 Dify 模板，再撤旧值。
