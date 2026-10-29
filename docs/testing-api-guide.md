> 本退役分支已移除旧标准诊断接口；C端检测走业务工作流，管家走节点，统一使用 QA 回读。详见[退役说明](runtime-retirement.md)。

# AI-Ops 功能测试接口文档

版本：2026-10-28｜适用对象：产品、测试、前端联调｜接口前缀：`/v1`

本文可单独转发。覆盖C端只读客服、管家节点诊断及相关用户接口；请求示例使用占位凭据和合成ID。
接口字段按代码基线 `1370e11b` 核对，核心样例经临时Gateway实际请求验证；不是本次重新执行了生产验收。

在线查阅：<https://api.mall.qushiyun.com/v1/docs>。
OpenAPI 3.1导入地址：<https://api.mall.qushiyun.com/v1/openapi.json>。
在Apifox/Postman选择“导入 → OpenAPI/Swagger → URL”，填上述JSON地址；也可下载JSON文件导入。
规范匿名可读，调用业务接口仍须个人登录态。在线版本随Gateway部署更新，上线结果见本轮验收记录。

## 1. 测试前准备

### 1.1 地址与当前范围

| 项目 | 地址/状态 |
|---|---|
| API基址 `BASE_URL` | `https://api.mall.qushiyun.com`，不要额外拼 `/aiops` |
| C端H5登录页 | `https://ulink.h5.mall.qushiyun.com/shopPackage/pages/login/index` |
| C端AI客服 | `https://ulink.h5.mall.qushiyun.com/aiPackage/pages/chat/chat` |
| C端WF01 | 已启用；知识指导、本人订单状态/账务、连续问答、来源/媒体、多语言 |
| 管家节点接口 | 2026-10-28记录为持续开放：`operator` / `node` / active；单订单CPMS详情与监测 |
| 管家Dify预览 | 正常公司登录后的原生预览已连Java；公司管家App正式页面部署仍是独立交付项 |
| 图片上传 | 现役未开放，不纳入本轮成功验收；回答内已有图片/视频可测 |
| 健康报告、站点分析 | 已实现的独立能力，按本轮任务与数据源条件测试；不因WF01通过就视为通过 |

以上API基址是现役入口，不是一次性沙箱。普通功能用小量请求；压测、破坏性异常、改库、撤回/发布等操作在指定测试环境执行。
提交一次问答/诊断可能消耗真实模型额度或读取真实业务数据，不做无界循环和无条件自动重试。
环境或版本变化时记录新的基址、运行修订和应用发布版本，不把本页日期当作实时健康证明。

### 1.2 账号与测试数据

准备C端账号A（有本人订单）、C端账号B（与A不同主体），以及管家账号O（有预期管理范围）。
准备A的至少两张可见订单、一张对B不可见的订单，以及O范围内/范围外的订单。
账号角色、租户、订单范围由测试负责人确认；不要拿A的凭据仅替换请求中的用户字段来冒充B。
凭据过期后正常重新登录，不扫描缓存找替代会话，不自行延长有效期。

### 1.3 请求头：两种身份不能混用

| 请求头 | C端 `consumer` | 管家 `operator` |
|---|---|---|
| `third-session` | 必填，当前C端登录态 | **不发送** |
| `Authorization` | 不手工发送服务令牌，入口代理处理 | `Bearer <当前公司个人access_token>` |
| `X-Business-Entry` | `consumer` | `operator` |
| `Accept-Language` | 建议显式填写，例如 `zh` | 同左 |
| `Content-Type` | JSON请求填 `application/json` | 同左 |

C端：正常H5登录后，在浏览器开发者工具 Network 中打开一条 `/v1/faq/catalog` 或 `/v1/assistant/questions` 请求，
取自己当前的 `third-session` 请求头。公网发送的是这个名字，不是 `X-Third-Session`。
管家：正常公司管家账号密码登录后，从自己登录流程/接口请求取得个人Bearer；Dify Cookie不能当App Bearer使用。
不要把开发机fixture令牌、AI-Ops服务key、Dify应用Service key交给测试同事代替公司个人身份。
来源认证头 `X-AIOps-Source-Key` 由可信代理添加，测试方不编造。如果合法登录仍401，先核入口链和令牌有效性。

凭据只放测试工具本地秘密变量，不放共享环境文件、截图、邮件正文或导出的集合；本文件不包含真实凭据。
以下多行curl适用于bash/zsh/WSL；Windows原生PowerShell请在Apifox/Postman的cURL导入中粘贴请求，
或自行改为PowerShell的变量和续行语法。仅把curl改为curl.exe不能直接运行下面的bash脚本。

```bash
# 将尖括号值替换为本轮真实测试数据；不要把填好凭据的脚本共享出去。
BASE_URL='https://api.mall.qushiyun.com'
C_SESSION='<自己的当前C端third-session>'
OP_TOKEN='<自己的当前管家公司个人access_token>'
LANGUAGE='zh'
ORDER_A='<账号A可见的订单号>'
OP_ORDER='<管家O管理范围内的订单号>'
QA_ID='<从POST响应复制qa_id>'
DIAGNOSIS_ID='<从POST响应复制diagnosis_id>'
CONVERSATION_ID='<从POST响应复制conversation_id>'
```

## 2. 最短冒烟流程

1. 正常登录并准备C_SESSION，调用固定FAQ目录确认身份链得到200 JSON。
2. 提问“怎么开始充电？”，记录POST的 `type`、`qa_id`、`conversation_id`。
3. 每次等待 `retry_after_ms` 后GET同一个qa_id，直到终态；核正文、来源及语言。
4. 新问题“查我的最新订单”，同样POST/GET，核查确为本人订单且来源为 `cpms.chargingInfo`。
5. 管家另用OP_TOKEN与明确OP_ORDER提交，记录qa_id并GET；不能拿C端会话测试管家。
6. 运行第9节的权限、取消、多语言等用例，留请求时间/ID和脱敏结果。

```bash
# 第1步：身份/目录冒烟。200 HTML不是通过，必须是JSON且含entries。
curl -i --max-time 30 "$BASE_URL/v1/faq/catalog" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer' \
  -H "Accept-Language: $LANGUAGE"

# 第2步：不填写shortcut_code，测试普通WF01知识问答。
curl -i --max-time 90 "$BASE_URL/v1/assistant/questions" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer' \
  -H "Accept-Language: $LANGUAGE" -H 'Content-Type: application/json' \
  --data-raw '{"question":"怎么开始充电？"}'

# 第3步：把返回的qa_id写入QA_ID后，查询该任务。
curl -i --max-time 30 "$BASE_URL/v1/assistant/questions/$QA_ID" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer' \
  -H "Accept-Language: $LANGUAGE"
```

`--max-time`是测试工具的等待预算，不是服务端完成承诺。POST超时表示结果未知，不能据此立即重发；
先核页面/历史记录/关联日志是否已建任务。GET尚为queued/running时只继续GET，不重复POST。

## 3. 用户接口清单

所有路径均拼在BASE_URL后。不要把整份清单改成H5静态域名执行：H5同源转发只放行页面需要的部分路径。

| 编号 | 方法及路径 | 用途 | 成功响应 |
|---|---|---|---|
| A01 | `POST /v1/assistant/questions` | C端客服、快捷动作、管家节点入口 | 202异步或200同步；见第4节 |
| A02 | `GET /v1/assistant/questions/{qa_id}` | 读本次问答/管家节点结果 | 200，检查status |
| A03 | `POST /v1/assistant/questions/{qa_id}/cancel` | 取消该问答/节点任务 | 200，返回当前任务状态；无请求体 |
| A04 | `GET /v1/assistant/questions?limit=50` | 当前主体问答历史索引 | 200，type=qa_list、count、questions |
| C01 | `GET /v1/conversations?limit=50` | 当前主体会话列表 | 200，count、conversations |
| C02 | `GET /v1/conversations/{conversation_id}` | 会话轮次 | 200，turns含问题与已完成答案；失效可拒绝 |
| C03 | `DELETE /v1/conversations/{conversation_id}` | 删除本账号测试会话 | 200，type=conversation_deleted |
| F01 | `GET /v1/faq/catalog` | 固定FAQ目录 | 200，entries |
| F02 | `GET /v1/faq/recommendations` | 推荐问法 | 200，recommendations，含question_id/title/sort |
| F03 | `POST /v1/faq/answer` | 固定FAQ同步取答 | 200，question/answer/format；无需轮询 |
| U01 | `GET /v1/shortcuts` | 当前入口已发布快捷动作 | 200，type=shortcut_list、shortcuts |
| U02 | `GET /v1/assistant/starters` | 开场白与推荐语 | 200，opening、suggested_questions及来源标记 |
| U03 | `GET /v1/banner` | 入口横幅配置 | 200，type=banner、banners；空数组可合法 |
| M01 | `GET /v1/media/{signed_id}` | 读取本轮返回的签名媒体 | 200媒体字节；Range可206，失效403/404；非法Range为416 |
| O01 | `GET /v1/orders/{order_no}/access` | 订单授权探针，不读正文 | 200，accessible=true；不可见/不存在404 |
| H01 | `POST /v1/health-report-jobs` | 充电健康报告 | 202，job_id；不等于故障根因诊断 |
| H02 | `GET /v1/health-report-jobs/{job_id}` | 健康报告轮询 | 200，report在完成后可用 |
| R01 | `POST /v1/standard/analyses` | 管家站点时间窗聚合；独立能力 | 202，analysis_id |
| R02 | `GET /v1/standard/analyses/{analysis_id}` | 聚合报告轮询 | 200，card/result/status |

A04/C01/D04的limit范围1–200、默认50。历史索引不保证包含仍可交付正文，要用详情接口重验；
不要只看列表有旧记录就断言权限/TTL绕过。删除会话不是取消在飞任务，也不是删除公司业务订单。

## 4. 统一提问 A01

### 4.1 请求字段

`POST /v1/assistant/questions`，JSON对象。不接受额外的tenant_id/user_id/platform等字段。

| 字段 | 必填 | 约束/用途 |
|---|---|---|
| question | 是 | 字符串，1–4000字符；不要只发空白 |
| order_no | 否 | 1–128字符，字母/数字/`_ . : -`；明确选择订单时填 |
| conversation_id | 否 | 原POST返回的 `conv_...`；新对话省略，不编造 |
| shortcut_code | 否 | `/v1/shortcuts`返回的code；普通WF01问题省略 |
| photo_id | 否 | 仅在图片能力明确开放并上传成功后填；现役测试省略 |

普通C端请求语料：

```json
{"question":"帮我查看最新的订单的情况"}
```

```json
{"question":"查询这笔订单的状态","order_no":"ORDER-EXAMPLE-001"}
```

```json
{"question":"查询我的最新订单退款记录"}
```

```json
{"question":"它现在结束了吗？","conversation_id":"conv_11111111111111111111111111111111"}
```

订单和会话ID均替换成当前账号的实际值。每条独立正例可以省略conversation_id创建新对话；
连续场景必须等待前轮终态后再带原ID。有效前轮可指代该单，失效后“那笔呢”需澄清；明确新查询可重新取数。
不要以关键词判断路由：问“订单状态是什么意思”是知识问题，问“我上一个订单现在什么情况”才查本人订单。

### 4.2 先看type，再决定是否轮询

| HTTP与type | 含义 | 下一步 |
|---|---|---|
| 202 + qa | 问答或管家节点已创建 | GET A02，使用qa_id |
| 202 + diagnosis | 旧订单检测/诊断已创建 | GET D02，使用diagnosis_id |
| 202 + analysis | 运营复盘快捷动作已创建 | GET R02，使用analysis_id |
| 200 + faq | 旧分流直接取到固定FAQ | 读answer，不轮询 |
| 200 + clarification | 需要补信息或返回安全/人工/能力说明 | 读message，不轮询；不是“任务成功” |
| 4xx/5xx | 本次请求拒绝/故障 | 读error.code/message；不猜错误原因 |

202只表示受理；极快任务也可能在首次响应已终态，应以status为准。
固定FAQ的F03响应本身不承诺有type字段，其字段以第6节为准。

### 4.3 QA创建与完成示例

下列来自临时Gateway实际响应，ID作格式保持的替换，问题和正文为合成资料；不保证模型逐字复述。

```json
{
  "type":"qa","language":"zh","qa_id":"qa_11111111111111111111111111111111",
  "question":"如何开始充电？","status":"queued","retry_after_ms":1000,
  "result":null,"error":null,
  "conversation_id":"conv_11111111111111111111111111111111","turn_no":1
}
```

```json
{
  "type":"qa","language":"zh","qa_id":"qa_11111111111111111111111111111111",
  "question":"如何开始充电？","status":"completed","retry_after_ms":null,
  "result":{
    "blocks":[
      {"kind":"text","text":"请按设备屏幕提示操作。"},
      {"kind":"reference","reference_id":"chunk-1","title":"充电说明.md"}
    ],
    "retrieval_status":"found"
  },
  "error":null
}
```

当前WF01与管家节点正文通常位于 `result.blocks`；不要只取旧通用问答的 `result.text`。
text块读text，reference块读reference_id/title，image/video块读取实际返回的media对象和URL。
不要自行拼接媒体路径、替换签名或把知识库原始内部地址暴露给前端。

### 4.4 实时查询与媒体判据

- 状态回答须包含本轮订单号、状态、`cpms.chargingInfo`与UTC观察时间；不从状态推断故障原因或付款结果。
- 账务须来自 `cpms.orderDetail`，保留金额/退款记录及“不证明银行到账”的限制；缺失不当零，不猜币种。
- 实时答案常为 `retrieval_status=not_found`：表示本轮没有知识检索，不代表订单不存在。
- 管家节点当前 `retrieval_status=unavailable` 表示没有提供知识检索；`status=completed`且CPMS证据齐全可为有效报告。
- 知识路径found须有本轮来源；not_found是无适用资料，unavailable是资料服务不可用，不能互换结论。
- 旧问答也可返回limited（部分检索受限），保留实际状态与限制，不自行改成found。
- 视频/图片展示使用本轮返回URL。相对URL按API基址解析；签名GET无需附公司令牌，过期/伪造链接应拒绝。
  外部视频仅访问响应给出的地址，不附带会话/Bearer；拿到200不能证明连续播放或地区网络可用。
- 更新所选库内文档后等待索引，发新问题应读新资料；无需重新发布。更换所选库/工作流才需要管理发布。

## 5. 轮询、取消与会话

| status | 处理 |
|---|---|
| queued / running | 继续GET；按retry_after_ms等待，通常1000ms |
| completed | 停止轮询；检查result或report，再判断业务正确性 |
| inconclusive | 停止轮询；有报告但无法确定结论，不等于接口失败；常见于旧诊断 |
| failed | 停止轮询，保留error；服务失败、缺证和测试环境问题分类记录 |
| cancelled | 停止轮询，不显示该任务成功正文 |
| expired | 停止轮询；作业/结果超时，不自动重新POST |

WF01及节点结果另受短期授权（当前约180秒）和绑定限制；即使存储保留更久，详情也可410/404拒正文。
只要HTTP不再200，应停止按成功正文处理。取消与完成竞态时，取消接口可能返回已经完成的终态，不要求强改为cancelled。

```bash
# 取消QA：同主体、同入口；不传请求体。
curl -i --max-time 30 -X POST "$BASE_URL/v1/assistant/questions/$QA_ID/cancel" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer'

# 获取连续会话；不用于轮询运行中的答案。
curl -i --max-time 30 "$BASE_URL/v1/conversations/$CONVERSATION_ID" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer'
```

取消成功的典型响应：

```json
{"type":"qa","language":"zh","qa_id":"qa_11111111111111111111111111111111",
 "question":"查我的最新订单","status":"cancelled","retry_after_ms":null,"result":null,"error":null}
```

会话详情含conversation_id/business_entry/agent_version_key/active_order_no/is_generating/创建更新时间/过期时间和turns。
每个turn有turn_no/kind/question/answer/created_at；历史过期或失去权限可能整体拒绝，不应伪造旧答案补回。
`POST /v1/conversations`另要求agent_version_key，但普通WF01无需先调它，直接A01自动创建即可。
`POST /v1/conversations/{id}/active-order`是旧会话选单辅助面，不能替代当前POST中的订单授权。

## 6. 固定FAQ、快捷动作与旧诊断

### 6.1 固定FAQ

```bash
# question_id从当前入口目录选取；示例ID须确认存在。
curl -i --max-time 30 "$BASE_URL/v1/faq/answer" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer' \
  -H "Accept-Language: $LANGUAGE" -H 'Content-Type: application/json' \
  --data-raw '{"question_id":"consumer.faq.q001"}'
```

响应字段：platform、available_platforms、language、faq_version、question_id、question、answer、format。
目录/推荐只证明固定内容可读取，不能证明WF01调用了动态知识库、CPMS或模型。
固定FAQ的question_id与知识库文档编号不是同一回事；跨入口FAQ通常404 FAQ_NOT_FOUND。

### 6.2 快捷动作

`GET /v1/shortcuts`返回code、language、intent、requires_order、label、description、question_template、jump_path等。
不写死数量和code：jump_path有值时由App导航，不POST提问；requires_order=true先选择可见订单。
测试订单检测示例：

```json
{"question":"请检测选定订单的充电异常","order_no":"ORDER-EXAMPLE-001","shortcut_code":"smart_diagnosis"}
```

本分支的 C端/管家订单检测均返回 type=qa；原生结果按所属业务路线交付。
`case_exploration`等宣传动作是其他路径，不以其检索失败证明WF01动态知识失效。

### 6.3 退役后的结果回读

C端与管家订单检测均经统一助手返回 QA 资源，按 `qa_id` 读取/取消。
旧标准诊断的创建、查询、列表和取消端点已删除；不再使用 `diagnosis_id`。

## 7. 管家节点诊断

管家新路线必须从A01进入，使用个人Bearer和明确订单。不能用“我的上一个订单”代替选单，
不能把Dify预览Cookie或C端third-session发到该路径。普通员工受站点范围约束，合法主账号按公司既有租户权限。

```bash
curl -i --max-time 90 "$BASE_URL/v1/assistant/questions" \
  -H "Authorization: Bearer $OP_TOKEN" -H 'X-Business-Entry: operator' \
  -H "Accept-Language: $LANGUAGE" -H 'Content-Type: application/json' \
  --data-raw "{\"question\":\"检查选定订单\",\"order_no\":\"$OP_ORDER\"}"

# QA_ID必须改成上一个管家POST的返回值。
curl -i --max-time 30 "$BASE_URL/v1/assistant/questions/$QA_ID" \
  -H "Authorization: Bearer $OP_TOKEN" -H 'X-Business-Entry: operator'

curl -i --max-time 30 -X POST "$BASE_URL/v1/assistant/questions/$QA_ID/cancel" \
  -H "Authorization: Bearer $OP_TOKEN" -H 'X-Business-Entry: operator'
```

管家POST典型形状：

```json
{"type":"qa","language":"zh","qa_id":"qa_22222222222222222222222222222222",
 "question":"检查选定订单","status":"queued","retry_after_ms":1000,"result":null,"error":null}
```

缺订单可返回200 clarification、code=CLARIFY_ORDER_REQUIRED、missing_fields=["order_no"]，不创建任务。
完成正文应说明订单详情和有限监测证据，来源含 `cpms.orderDetail` / `cpms.chargingMonitor`。
监测当前最多首批50条，无完整排序/时间线保证；0条是缺证，不等于设备正常。
无权/数据源/工具授权等管家拒绝可403 OPERATOR_*，撤回历史正文可410 OPERATOR_RESULT_RETRACTED；
不要要求与旧诊断所有404分支逐字相同。检查拒绝正文不暴露订单或他人数据。

## 8. 健康报告与站点聚合（按专项测试）

### 8.1 健康报告 H01/H02

请求体仅 `{"order_no":"ORDER-EXAMPLE-001"}`，按账号权限选择订单。
POST返回job_id/order_no/rule_version/language/status/retry_after_ms/report/error及时间字段；
GET `/v1/health-report-jobs/{job_id}`。完成读report.summary、indicators、completeness、source_summary等实际返回字段。
无独立取消接口；不要将job_id传给QA或诊断cancel。

```bash
curl -i --max-time 90 "$BASE_URL/v1/health-report-jobs" \
  -H "third-session: $C_SESSION" -H 'X-Business-Entry: consumer' \
  -H "Accept-Language: $LANGUAGE" -H 'Content-Type: application/json' \
  --data-raw "{\"order_no\":\"$ORDER_A\"}"
```

### 8.2 站点聚合 R01/R02

仅operator，使用个人Bearer。card取failure_rate/offline/fault_code/payment，缺省failure_rate；
site_ids可选、最多1000项，建议测试指定少量授权站点，不传空数组；window_start/window_end必填ISO 8601时间。
显式带 `+08:00` 或 `Z`，结束晚于开始，采用半开区间[start,end)。先用短时间窗，不能把不传site_ids理解为无权限限制。

```json
{"card":"failure_rate","site_ids":["SITE-EXAMPLE-001"],
 "window_start":"2026-10-28T00:00:00+08:00","window_end":"2026-10-28T01:00:00+08:00"}
```

POST返回analysis_id/kind/card/site_ids/window_start/window_end/rule_version/language/status/retry_after_ms/result/error。
GET R02轮询同一analysis_id（常见前缀为an_，必须从返回值复制）；该接口无独立cancel，不把analysis_id传给其他资源的cancel。
它使用独立聚合规则/数据源，不是Java两项CPMS工具。代码存在不等于目标环境的卡片已完整验收。

## 9. 建议测试用例与通过标准

| 用例 | 步骤 | 判定 |
|---|---|---|
| T01身份冒烟 | 正常登录→F01；再去掉凭据 | 正例200 JSON；负例401/认证拒绝，不出现业务正文 |
| T02知识指导 | A01问已存在资料的操作问题→A02 | completed、有正文/引用，答案符合资料 |
| T03最新/指定单 | A查询最新，再指定另一张本人单 | 两次都是真实各自目标；不能总回同一旧单 |
| T04账务 | 问本人订单退款记录 | 有同轮金额/来源，不声称已替用户退款或银行到账 |
| T05连续会话 | 完成后带conversation_id追问 | 可接续有效前文；明确新问题不误用旧订单 |
| T06多语言 | 逐一设语言，测试中文与本语输入 | 查看助手正文；不能仅因language字段相同就判通过 |
| T07知识更新 | 在授权测试库增改资料、索引完成、发新问题 | 不重新发App即可检索；删资料后新问题不从旧审核副本回落 |
| T08媒体 | 有资料视频/图片的问题→读取返回链接 | 来源可信，签名媒体正常；失效/伪造拒绝，播放器另验 |
| T09跨用户 | B查A的订单、QA、会话、旧诊断ID | 均拒绝或不交他人数据；ID不存在/无权不提供存在性线索 |
| T10取消 | A01创建后立即A03，再A02 | 未完成任务取消后无成功正文；若已完成，返回原终态 |
| T11非法参数 | question缺失、超长、添加tenant_id/user_id | 422 INVALID_REQUEST，不产生任务；不能只看HTTP 200 |
| T12人工/危险/写请求 | 明确要人工、危险描述、要求退款执行 | 正确引导/避险/能力说明，不声称已接通或执行写操作 |
| T13管家节点 | O明确选单→A01→A02；再测缺单、越权 | 有限CPMS证据；缺单澄清、越权拒绝，不借共享身份 |
| T14历史失效 | 等有效期到期后GET；测试环境撤回后GET | 旧正文被拒绝；不通过本地缓存继续交付 |
| T15专项报告 | H01/H02或R01/R02 | 对应ID和报告字段正确，结论与业务证据核对 |

支持语言：zh/zh-Hant/en/de/fr/es/pt/vi/mn/th/km。页面当前9项（无德/法选项）；
zh-CN→zh，zh-HK/zh-TW→zh-Hant，portuguese→pt，spanish→es。未知语言按既有规则回退zh。
当前WF01和明确订单诊断不应因泰/高棉语统一拒绝；旧无明确订单关键词路径仍有适用范围限制。

## 10. 常见响应与故障定位

```json
{"error":{"code":"INVALID_REQUEST","message":"request validation failed","retryable":false}}
```

| 现象 | 应怎么判断 |
|---|---|
| 401 | 登录态缺失/过期/入口不匹配；先正常登录，不寻找服务key替代 |
| 403 | 当前入口/主体/工具不允许；管家OPERATOR_*需结合代码与任务范围 |
| 404 | 不存在、无权、失效媒体或未启用能力等不同原因；保存error.code，不直接写“接口没开发” |
| 409 | 会话在生成、发布版本变化、前轮失败/取消等冲突；按提示等待或新建对话 |
| 410 | 旧结果/绑定/授权有效期失效或撤回；停止展示正文 |
| 422 | JSON/字段/长度/格式校验失败 |
| 429 | 频率或资源限制；停止批量提交，按返回提示处理 |
| 503 | 分类、依赖或授权服务不可用；不能退回FAQ冒充查询成功 |
| 200但status=failed | HTTP读取成功，任务失败；按error记录 |
| 200 HTML/网关错误页 | 可能走了静态站点或错误代理，不能当JSON接口成功 |
| 跳转按钮没有POST | jump_path类型本来应导航，不创建问答 |
| retryable=true | 表示可人工决定重试，不是客户端无条件自动重发的授权 |

面向用户的error.message部分会本地化，其余保留固定错误说明；断言优先用HTTP、type/status、error.code及是否泄露正文，不匹配模型逐字答案。
反馈至少包含：基址/版本、北京时间或明确时区、入口/语言、脱敏角色、步骤、请求体、HTTP、type/status/error.code、
QA/diagnosis关联ID和脱敏截图。共享材料删除Cookie/Authorization/third-session、完整个人资料及无关订单数据。

## 11. 管理流程与内部接口边界

SOP编辑/试用/发布/撤回/恢复、换库在指定Dify测试应用用正常公司登录执行，不能用普通C端凭据直接调管理接口。
管理操作会改变用户路线，不在生产应用盲跑测试脚本。以下列方法/输入用于与维护人员对照，不提供管理秘密：

| 管理路径 | 方法/输入 | 测试关注点 |
|---|---|---|
| `/v1/dify/versions` | POST `{app_id}`，管理认证 | 版本列表/当前state；不是GET |
| `/v1/dify/trial` | POST `{app_id,order_no,question}`，试用认证及发起人上下文 | 202/trial_id；草稿独立执行不切正式版 |
| `/v1/dify/trial/{trial_id}` | GET，试用认证及发起人上下文 | completed结果/失败原因 |
| `/v1/dify/trial/{trial_id}/cancel` | POST `{app_id}`，试用认证及运营会话 | 仅管家节点试用；不能当作所有Dify试用或App QA的通用取消 |
| `/v1/dify/publish` | POST `{app_id}`，管理认证 | 保存未生效，发布才改变正式路线 |
| `/v1/dify/retract` | POST `{app_id}`，管理认证 | 后续任务/正文拒绝，保留版本记录 |
| `/v1/dify/restore` | POST `{app_id,version_no}`，管理认证 | 显式恢复所选版本；库内资料不随工作流倒退 |

管家Dify原生预览要求每条消息有明确唯一订单号，走公司会话→单订单授权→Java试用，不支持“最新单”自动代选。
`/v1/agents`、shortcut-admin、知识源写入、注册设备、`/v1/internal/*`和`/v1/operator-tools/*`属于独立管理/内部面，
不向普通功能测试方分发服务密钥或单轮grant来直接调用。知识/CPMS回调是否发生通过A01/A02与维护侧运行证据核对。
`/v1/orders/{order_no}/charging-status`与delegated是旧独立状态面，受单独开关控制；测试WF01实时查询使用A01，
不能因旧状态面404就断言WF01不可用。普通测试不触发重算、退款、停充、改单或自动建单。
