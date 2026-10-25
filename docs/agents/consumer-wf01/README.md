# WF01 C 端业务完善

目标是原 App 内的只读客服：操作指导、真实状态、连续追问、图文/视频和人工入口。
实现顺序为内容基线 → Chatflow/会话 → 受控只读能力 → 媒体/人工 → 图片输入 → 原App整体验收。
现有状态节点路线保持原行为，WF01通过等价业务验收之后才切换。

## 内容包生成

```bash
uv run python tools/build_consumer_knowledge.py <私有原包目录> --output <新的私有输出目录>
```

输入：运营工作簿、`knowledge_markdown/`、`MEDIA_ASSET_MANIFEST.csv`、`00_System_Prompt.md`。
复用仓库已有OOXML读取器，不安装新的Excel运行依赖。工作表按名称读取；答案与Markdown逐字比对，
媒体CSV与工作簿比对；重复/未知ID、缺必填字段、错正文拒绝，写入前完成所有校验。

输出为草稿：每问答中英文各一文件；`content.json` 保留来源摘要、原始行、内部依赖/风险/配置、
媒体规划与双向冲突。缺媒体地址不造URL；有地址也不自动当作已核验素材。
两个关联方向不一致时保留双方事实而非擅自取并集。既有输出拒绝覆盖。
正文只投影标准答案，不将内部工具名或升级规则直接交给用户。

`workflow_scope`只是后续能力核对清单，不是可执行白名单：所有只读候选仍需实际接口与授权核验。
启动/停止/下单/隐私请求/安全事件建单/工单创建列入首轮禁用。
原System Prompt保留为来源，不能原样启用其中的建单指令。

## 原App完成判据

以[业务验收计划](qa-plan.md)执行：实际Chatflow编排，原App连续会话、适用知识、
API异常/越权、安全优先、真实可播放媒体、实际人工入口逐项留证。导入成功不能代替这些结果。
私有材料不提交，仓库只收工具、脱敏说明和合成测试。业务问答仍须按租户规则审核。

## Chatflow Service API复跑

`uv run python tools/verify_consumer_chatflow.py --out <私有报告.json>`
启动一次性Dify1.17.1和官方provider，合成模型驱动真实Chatflow三轮；
无宿主服务端口、无生产凭据。依赖已有Docker镜像及官方插件可下载。结束清理自己的容器与卷。
`dify_chatflow.py`同时核对workflow_finished和message_end，固定版本与会话；调用方仍需实现范围授权。

## 原提问接口的Chatflow接线（默认关闭）

`AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED=1`启用服务端登记的`consumer_service`场景。
沿用`CHAT_EXECUTION_BINDINGS_FILE`，engine=dify，另需app_id、workflow_id、workflow_key_file、
agent_id、agent_version_no及environment/tenant_id/entry=consumer；只读工具尚未在此片开放。
首次POST返回conversation_id，客户端后续原样带回；它是Gateway会话，不是Dify会话ID。
失败/取消或发布版变化后的会话返回409要求新会话；原QA与会话正文过期/换绑后拒绝交付。
旧状态查询场景与开关独立，未配置WF01时保持既有路线。本片仅供隔离接线验证，不能当完整客服上线。
