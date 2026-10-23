# 客户端本人订单状态查询

用户授权本片同时支持指定订单与本人最新一单，不修改公司侧仓库。
只使用既有 `POST /v1/assistant/questions` 和 QA 回读；状态是确定性业务读取，模型调用为零。
这不是 App→Dify/节点的引擎切换，也不代表 M 系列整线完成。**未完成业务验收**。

## 行为与范围

- `{"question":"查询我最新一单的状态"}`：本人范围过滤后按 `created_time DESC,id DESC` 选第一单。
  使用现有分页查询（内部最多读两行），只向 CPMS 查询选中的一单；工单归属沿用 `consumer_id` 判据。
- `{"question":"查询这个订单的状态","order_no":"<订单号>"}`：显式号优先；正文中的订单号也沿用现有提取器。
  无号且没明确说最新则要求补订单号，不猜历史订单。
- 入口只接受显式 consumer、非代查 self 和有效 C 端身份。助手仍要求 `aiops:diagnoses:write`，
  此路径另问同一身份解析器验证 `aiops:orders:read`，范围指纹必须一致。模型或请求参数不能提供租户/用户。
- 返回前再次核对订单可见性。成功响应 `202 type=qa,status=completed,result.blocks`，随后 GET 200
  重验本人权限和证据；App 原有 `fetchAssistantQa` 与 blocks 渲染可直接消费。超时无自动重发。
- 明确的简体/繁体/英文状态查询才进入本片；诊断、扣费原因、写动作、快捷动作及其他语言保持原路由。
  这是保守措辞匹配，不承诺任意问法；未知状态仍 unknown，不推断成正常或异常。
- 缺身份 401、错误范围/读权限 403、不可见/无本人订单 404、授权源或证据失败 503、CPMS 坏结果 502。
  相关回答和错误带 `Cache-Control: no-store`；读取时刻为 UTC，来源为 `cpms.chargingInfo`，不代表物理枪实时状态。

## 观察

每次**候选**个人状态查询（启用 + consumer + 无快捷动作 + zh/zh-Hant/en + 命中状态问法）
写一行 `INFO aiops.assistant_question assistant_question_result …`，带服务端生成的
`request_id`、`stage`、`outcome`、`http_status`、`error_code`、`tenant_hash`、
`qa_id` 与 `duration_ms`。`qa_id` 成功时有值；**证据落盘失败（503）时也可能有值**（QA 行先建好了），
拒绝则写 `-`。补充：`outcome=completed` 的 200 clarification 是 `qa_id=-`，
它**没有查到订单**，只是要求补订单号。

这一行是变更单停止条件可判定的前提。字段、计时范围、看不到的部分与观察命令见
[观察契约](observability.md)。普通问答、功能关闭、身份未建立**不写**这一行。

## 配置和部署

配置模板见 `.env.example`，进程环境优先于服务端私有配置文件：

```dotenv
AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=false
AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=<本环境租户映射或既有个人状态地址>
AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=<逻辑执行环境>
```

启用必须校验会话、SQL 和 CPMS 属于同一客户环境。运维 `OPS_*` 配置不能作为此能力的回退；
共享个人状态地址同时启用已有 GET 直读入口，不能把这个副作用隐去。M3 委派仍需独立配置，不随本片启用。
合并制品经既有 CD 部署；会话表新增可空 `status_qa_id`，旧行不回填，功能关闭时也会迁移列。
本轮不设置生产开关、不使用旧 OPS 验收备份覆盖配置、不重试登录或制造订单样本。

部署后依 [41 手册](../env-41-runbook.md) 与 [首片只读核对方法](../dify-ops-first-slice/README.md)
执行 `dev-host show 41` / `dev-host exec 41 --allow-service-exec -- '<只读核对>'`：
核对 health/build、进程与私有文件中开关布尔值、个人地址/执行环境是否配置；只输出布尔值和摘要。
SQLite `mode=ro` 查 `PRAGMA table_info(conversation_turns)` 确认列；不要构造 manager 来“只读”查库。

回退优先将开关设 false 并按部署流程生效，新查询回到旧路由、既有快照回读拒绝。
保留新版本的回读守卫与列；**启用产生记录后直接回到旧代码会让旧会话逻辑重新使用快照**，
需要先独立处理这些记录，不能把代码回滚与停用等同。尚未启用时无新快照，该限制不触发。

## 证据、留存与复核

QA 作业按原范围指纹隔离。私有 `<data_home>/consumer-order-chat/<qa_id>.json` 为 0600，
含五字段快照、调用编号、范围指纹、快照/回答 SHA-256；正文不含车牌/上游完整订单/会话令牌。
摘要沿用 `sha256(json.dumps(value, sort_keys=True).encode())`，不承诺防具有写权限的操作者篡改。
证据写失败拒绝成功；存储失败保留失败记录并释放会话槽。会话的 `status_qa_id` 绑定作业，
回读重验权限、快照/答案摘要和会话正文一致性；状态轮次排除出后续模型上下文。
原有诊断轮次的历史权限局限没有被本片回填修复。

证据文件沿用运维私有证据留存，当前没有自动删除任务；QA 到期不等于文件已删除。
不要删除活跃 QA 的证据，否则回读会拒绝。现有 App 源快照有请求/响应 console 日志，
本片不改公司代码，不能宣称用户设备无数据留存；真实上线前须由发版方确认实际构建的日志行为。

复跑：

```bash
PYTHONPATH=$PWD/src uv run pytest tests/test_consumer_order_chat.py -ra
PYTHONPATH=$PWD/src uv run pytest tests/test_consumer_order_chat.py -k real_http -ra
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py
```

前两条启动临时数据根与回环 CPMS；真实 HTTP 用 Uvicorn，不用 TestClient 冒充网络。
SQL 混合样本用实际生产查询加 SQLite 方言适配，只证明范围/排序，不证明 MySQL 网络或真实会话。
Gateway 技能只证明启动及基础鉴权。正常登录本人样本、真实 App、工程师业务结论仍另验；
判据映射见 [QA](qa-plan.md) 和 [Feature](acceptance.feature)。
