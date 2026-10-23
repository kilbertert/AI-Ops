# 真实 H5 本人订单状态查询验收（2026-10-23）

承接 #976（实现）与 #992（H5 转发）。本片补上此前一直缺的那一步：**真实登录用户
在已部署的 H5 页面里查到本人订单状态**。

这里记录方法、判据与结果。**原始凭据、订单号全量、截图都在私有目录**：
`/home/claude/.cache/m3-autonomous/consumer-order-status-20261023/`（0700），不进仓库。

## 一、结论

| 项 | 结果 |
|---|---|
| 真实 H5「本人最新一单」 | **PASS** |
| 真实 H5「指定本人订单」 | **PASS**（另用第二张本人订单，证明不是回落最新） |
| 本人授权（consumer/self/非代查） | **PASS**（正式解析器） |
| SQL 独立对照（第一条确为最新） | **PASS** |
| CPMS 独立对照（状态码/语义一致） | **PASS** |
| 关联模型调用为零 | **PASS**（窗口日志 0、run 目录 0） |
| 缺会话 / 不存在订单 | **PASS**（401 / 404，均不泄露正文） |
| 停用后历史快照回读拒绝 | **PASS**（`CHARGING_STATUS_DISABLED`） |
| 临时配置恢复 | **PASS**（sha256 与窗口前逐字相同） |

**边界**：本片只证明 **真实 H5 上的个人订单状态查询闭环**。不代表原生 APK、故障诊断
准确率、App→Dify/节点切换或 M1–M4 整线完成。

## 二、开窗前先固定独立对照值

**这一步是判据的锚。** 先取一次预期值，再开窗比对；否则"看到什么记什么"，
判据会退化成"与实现一致就算对"。

用生产自己的路径取（不重写规则）：`resolve_query_scope(caller)` +
`scoped_mysql_source(...).list_orders(page=1, page_size=3)`。

结果：第一条 `created_time` 最大，`top3_is_latest_first=true`。这条记录成为
后面两条页面结果的比对基准。

## 三、开窗（只动三项）

```
AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=true
AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=<该租户的个人 CPMS，取自已注册同环境端点>
AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=41
```

- 候选配置先**只构造 `GatewayServerSettings` 做 `validate()`**，不构造 app
  （构造 app 会调用 `recover_interrupted_jobs`，把在飞作业标 failed）。
- 按产物身份写入（`--if-sha256` + `--artifact-sha256`），只 `systemctl restart
  aiops-gateway-41`。
- OPS 与 M3 配置逐项核对未变。

## 四、两条真实页面正例

驱动方式：Playwright + 真实 H5 产物 + 真实登录会话，经 `ssh -L` 到 41 本机 443
（保留原域名与证书）。**每条都从干净聊天上下文发起，请求体只有 `question`**。

判据（全部满足才算 PASS）：

| 判据 | 为什么 |
|---|---|
| POST 202、`type=qa`、**`status=completed`** | 走确定性路径是同步完成；`queued`/`running` 说明落回了普通模型问答 |
| 请求体无 `order_no`（第一条） | 否则"最新"会被旧单覆盖 |
| 正文含该订单号（指定单） | 证明按号走 |
| GET 200 JSON 且 `no-store` | 个人业务结果不缓存 |
| 页面正文含订单号、状态、`cpms.chargingInfo`、观察时间 | 页面真的渲染出来了 |
| 无"我无法查询订单"类话术 | 那是回落到普通模型路径的形状 |

结果两条全通过。**第二次特意换用另一张本人订单**：若两条都选中同一单，
就分不清"按号查询"和"回落到最新"。

## 五、证据链（内部标记是关键）

三条 QA 的 `internal_run_id` 全部是 `consumer-order-chat:<订单号>` —— 这是
**走本片路径**而不是模型路径的直接标记。私有快照
`<data_home>/consumer-order-chat/<qa_id>.json`（0600）含 `call_id`、
`scope_fingerprint`、`snapshot`、`answer_sha256`、`result_sha256`，订单号与状态码一致。

窗口内（03:20–03:26 UTC）：

- 网关日志里模型调用记录 **0** 条；
- 新建 run 目录 **0** 个；
- `standard_diagnoses` 新增 **0**；
- `agent_runs` 总数 **0**。

## 六、反例

| 场景 | 结果 |
|---|---|
| 无会话 | 401 `INVALID_ACCESS_TOKEN` |
| 真实会话 + 不存在的订单号 | 404 `ORDER_NOT_FOUND` |
| 真实会话 + 一个**不存在**的订单号 | 404 `ORDER_NOT_FOUND`，不返回正文 |
| 停用后回读窗口内 QA | 404 `CHARGING_STATUS_DISABLED` |

注：这一条实际上只是**不存在订单**反例 —— 用的号码既不属于该主体、也不存在。
它证明的是"不返回正文、不泄露"，**不是**"真实存在的他人订单被正确拒绝"。
后者没有授权样本，记为**未验证**。这两件事不要合并成一条结论。

## 七、恢复

- 只撤销本轮三个字段，**不整文件覆盖**（防期间其他会话改过别处）。撤销前后
  差异集为空，说明没有别人的改动被连坐。
- `gateway.env` 恢复后 sha256 与窗口前基线**逐字相同**。
- `production.env` 未动。
- OPS 端点仍有效、M3 两项仍为空、#992 的两份 nginx 制品 sha256 未变。

### 窗口期间的一次外部部署（必须记）

CD 在窗口内（03:26:39 UTC）部署了 #995 `42cae384`。它只改
`dify_console_publish.py`，与 consumer 订单查询无交集；**三条查询都在它之前完成**，
结果未受干扰。但它确实重启了一次 Gateway —— 所以本片的"恢复后重启"是在
已恢复的配置上做的，版本为 `0.1.0+42cae3841af7`。

## 八、复跑

```bash
# 开窗前：固定独立对照值（只读）
AIOPS_CHECK_SESSION=<session> python independent_cpms.py

# 两条页面正例（各自从干净上下文发起）
python status_probe.py <latest_order_no> latest
python status_probe.py <another_owned_order_no> specified
```

脚本在私有目录。`AIOPS_CHECK_SESSION` 用环境变量传：41 的 Gateway 单元开了
`PrivateTmp`，`/tmp` 落盘的文件 `runuser -u aiops41` 读不到。

## 九、顺带发现的生产缺口

**2026-10-23 观测**：`conversation_turns` 当时是 0 行，`status_qa_id` 非空行也是 0。

影响：`status_qa_id` 那条"会话绑定作业、回读重验权限与摘要"的守卫在本轮**没有被执行到**，
只有测试覆盖。它在代码里是对的，但这一轮没有流量走到它。
（这只写当前观测；"历史上从未传过 conversation_id"没有证据，不下这个结论。）要不要让页面传
`conversation_id`，是一个产品决定，不在本片范围。

## 十、操作偏差（如实记录）

验证"停用后回读拒绝"时，我**多发了一次状态提问**，违反本文档自己定的
「停用后不再提交状态问题」。它走零阶检索路径（`turn_count=0`、`tool_call_count=0`、
日志模型调用 0），没有消耗模型额度，但仍是多余作业，**不计入任何判据**。
正确做法是只做回读、不新提问。

## 十一、工具修复（本片）

上面引用的三个脚本在第一版里有**四个误报**，已在
[`tools/consumer-status/`](../../../tools/consumer-status/README.md) 修掉，
并由 `tests/test_consumer_status_tools.py` 逐条钉住（做过变异复验）：

1. `check_identity.py` 只看 `scope_type == "self"`，`delegated=true`、`orders:read` 被拒、
   范围指纹不同、没有本人订单，全都仍退 0；
2. `independent_cpms.py` 捕获 CPMS 异常后仍退 0，且用了不存在的 `caller.user_id`；
3. `status_probe.py` 用整页文本判订单号（会命中**用户自己的提问**），
   GET 匹配不到本次 `qa_id` 时回退到别的 GET；
4. 复跑环境（隧道、登录态路径、私有产物目录）与文档不一致。

**这四处缺陷不影响本节的业务结论**：本节的结论来自页面实际渲染出的回答、
SQL/CPMS 独立对照与私有快照，那些证据是本片重新取的。工具修复只改判据的严格程度。
但有一处**必须说清**：当时"指定本人订单"那条是用正文里的订单号触发的
（`request_has_no_order_no` 的检查对 `specified` 用例写成了"正文含号"），
它证明了按号查询 — 因为换号后回答跟着换了 — 但没有证明 `order_no` 字段路径。

常驻开放的决策材料见
[permanent-enable-decision.md](permanent-enable-decision.md)。

## 十二、工具返修（第二轮，#1004 之后的复核）

上一轮的工具有**判据对但没被执行**的缺陷：Codex 把「无本人订单」分支和
「CPMS 异常」分支**分别改成 `EXIT_PASS`**，27 条测试照样全绿 —— 它们只读常量和
自造变量，没驱动实际分支。

### 修了什么

| 项 | 上一轮 | 这一轮 |
|---|---|---|
| 测试驱动 | 只测抽出的纯函数与常量 | **调用真实 `main(...)`**，身份/SQL/CPMS 用隔离替身，断言退出码、报告结论、**业务调用次数** |
| 排序判据 | `(created_time, order_no)` | `(created_time, **id**)`，与生产 `ORDER BY created_time DESC, id DESC` 一致 |
| 空/单行/缺 id | 判"通过" | `unverified`，退出码 2 |
| 指定单不在第一页 | 判"不属于本人" | 按号 `get_orders` 核对归属 |
| 固定预期 vs 一致性 | 混在一起 | `comparison.state` 明确是 `not_compared` 还是 `compared` |
| GET 匹配 | 只看路径 | 路径**与响应体 qa_id** 都要对上 |
| 完成态 | 只看 POST | 另加 `get_completed`（`failed` 带旧 blocks 不算成功） |
| 回答完整性 | 有订单号+来源+时间 | 另加**状态**判据 |
| 请求体 | 缺失也通过 | `request_captured` 必须为真 |
| 助手区域 | 猜的 `.msg-list .msg-item` | 按现役产物改成 `.ai-msg`，并判 `region_is_not_user_bubble` |

### 变异复验（Codex 复现的三个）

| 变异 | 结果 |
|---|---|
| 无本人订单 → `EXIT_PASS` | `test_no_owned_order_is_unverified_and_does_not_pass` **转红** |
| CPMS 异常 → `EXIT_PASS` | `test_cpms_failure_is_unverified_not_pass` **转红** |
| 两者同时 | 两条**同时转红** |

还原后 29 全绿。上一轮这三处"同时变异仍全绿"。

### 一个被自己的测试抓住的真缺陷

状态词表原本含裸词 `charging`，它命中了来源标记 `cpms.chargingInfo` —— 于是
"只有订单号+来源+时间、没有状态"的回答也能过。改成 `status code` / `状态码` /
「充电中」等带上下文的形式。**判据词表里的裸词会空转，这又一次是本仓自己的坑。**

### 浏览器验证范围

选择器用**与现役产物同构的合成 DOM**在本地回放（API 全量 route 拦截，零生产提交）：

- `.ai-msg .bubble-text` → 命中 1，含订单号
- `.msg-list .msg-item`（旧猜测）→ **命中 0**
- 用户 `.user-bubble` 不冒充回答

**这不是**公司构建产物的真实运行，**不构成**新的真实业务验收。

### 本片生产影响

零：未开窗口、未提交真实状态问题、未调用模型、未新增查询作业、未改 `src/`/Nginx/生产配置。

### 顺带清掉的一处自己的问题

`h5-route-fix-evidence.md` 里我上一片写了测试账号明文，已改为"授权测试账号"。
**注意：工作树里清掉不等于 Git 历史里没有** —— 历史未重写，本片不擅自改写。
