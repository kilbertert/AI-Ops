# WF01知识内容导入与核对

本步骤只建立独立待验收库，不绑定正式客服，不修改旧库。
原件先用`tools/build_consumer_knowledge.py`编译并留文件摘要；每条FAQ合并中英文与问法，
文件名固定`KB-C-NNN.md`，供Gateway按本轮检索返回的文件身份匹配已审核内容。
内部API名、升级规则、媒体规划ID不放进用户答案正文。

经`dev-host`进入已声明服务宿主，运行时从Gateway配置取得现有kb-service入口，
租户固定为当前获准范围；不打印任何凭据。调用顺序：

1. `GET /kb/knowledge-bases`保存基线，确认目标名称尚不存在。
2. `POST /kb/knowledge-bases`创建独立草稿验收库。显式填写当前租户已配置的embedding_model，
   不沿用接口过时的默认模型名。名称、描述明确待验收。
3. 创建返回后立即私有保存库ID；请求失败先重新列库核对，不盲目重建。
4. `POST /kb/knowledge-bases/{id}/documents`，multipart的字段名为`files`，逐文件留摘要与文档ID。
5. `POST /kb/knowledge-bases/{id}/documents/parse`，body为`doc_ids`列表。
6. 分页读取`GET /kb/knowledge-bases/{id}/documents?page=1&page_size=100`，分开计数DONE/RUNNING/FAIL；
   上传数量或chunk_count不能代替索引完成。
7. `POST /kb/knowledge-bases/{id}/search`，以真实问法和top_k检索，核对返回docnm_kwd与预期FAQ。

上述写入是知识配置及索引，不是订单/退款/停充操作。解析与检索可能使用既有嵌入模型额度。
通过索引与召回不等于业务内容已批准；审批状态、适用规则和真实媒体仍按内容快照管理。
完成后复核正式agent的知识绑定未改，新库不进入当前用户回答；不自动删除原库或原件。
