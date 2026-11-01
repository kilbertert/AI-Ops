# T13 第 I 项补做：管家端（员工端）真实会话与站点范围

执行时间：2026-11-01（UTC 09:4x，北京时间 17:4x）。环境：41 生产网关。
前置记录：[`2026-11-01-t13-g1-i-d-evidence.md`](2026-11-01-t13-g1-i-d-evidence.md) 把本项记为
**未执行（缺凭证）**。那份结论**是错的**，本文订正。

## 一、上一轮的结论错在哪

上一轮写"库外资产没有员工端凭据，只有平台 Dify 与两租户消费者账号"。实际重读加密清单：

| 资产 | `kind` | 账号 | 用途 |
| --- | --- | --- | --- |
| `aiops-ulink-account-20261030` | **`account+password`** | `ulink` | ulink 租户的**账号口令登录** |
| `aiops-youlianchong-account-20261030` | `account+password` | `chongdian` | 优链充租户账号口令登录 |
| `aiops-ulink-phone-20261030` / `aiops-youlianchong-phone-20261030` | `phone+fixed-code` | 手机号 | 消费者端 |

`account+password` 正是管家 H5（tenant-app）的登录形态；`phone+fixed-code` 才是消费者形态。
上一轮把两类资产混成"只有消费者账号"，于是把一次**本可执行**的验收记成了缺条件。

同名的 `ulink` 账号在 2026-10-22 就在 `ulinkmanage.h5.mall.qushiyun.com` 上走过真实账号密码
登录（[`docs/validation.md`](../../validation.md) §"第 1 项闭环"）。凭据一直都在。

## 二、真实登录（页面，非注入）

页面 `https://ulinkmanage.h5.mall.qushiyun.com/pages/login/login`，两个输入框
（`text` + `password`）、协议复选框 `.u-checkbox`、提交按钮 `.submit`。填账号口令、勾协议、
点登录后：

```
POST /upms/token/login?username=ulink&password=<密文>&randomStr=...   200
localStorage.CLOUD_ACCESS_TOKEN = present
URL → https://ulinkmanage.h5.mall.qushiyun.com/
首页正文：「你好，ulink | 智能检测 | 累计总收益(USD) | … | 105/297 在线/离线（桩）」
```

口令由页面自己加密（`password=tjR+B82HD/…` 是前端 AES 结果），全程未在本机明文拼装请求。

## 三、员工端助手真实会话

首页点「智能检测」→ `…/aiPackage/pages/chat/chat`。该页底部默认是**语音**（`按住说话`），
先点 `.mode-btn` 切换键盘，出现 `input[type=text]` 与 `.send-btn`（发送）。

问「看下我最近的订单」，页面可见回答：

> 请提供或选择要查看的订单号，我才能读取该订单的充电明细和停止线索。

网络与库内记录一致：

```
POST /v1/assistant/questions                       202  qa_959e0140c223487da97babd8e81897a8
GET  /v1/assistant/questions/qa_959e0140…          200  （页面轮询若干次）
assistant_questions: status=completed  tenant_id=1942105476598861824
```

**服务端路由证据**（这是"走到了被测代码"的判据，不是形状判据）：

```
agent_runs.run_id        run_2FP8X-zrIQBYFvLHnaHWv1jCwHZJyBeIpBWpG_qbMg8
chat_binding             operator_order_diagnosis:production-41:1942105476598861824
route_revision           12
exec_config_state        matched
state                    succeeded
scope.effective_tenant_id 1942105476598861824
scope.caller             b_user_id=<脱敏>  username=<运营账号>  user_type=1
scope.permissions        ["aiops:diagnoses:write"]
```

即员工端请求命中的是**该租户的 operator 绑定**（`chat_route_bindings` 里
`production-41 / 1942105476598861824 / operator / operator_order_diagnosis = active, rev 12`），
不是消费者绑定，也不是未登记卡片。

## 四、站点范围与跨租户反例

### 4.1 `data_scope.type=all` 是什么意思

同一 run 的 `scope.data_scope` 是 `{"type":"all","organ_ids":[],"shop_ids":[],"site_ids":[]}`。
三个 ID 列表全空**不是**"越权到平台"，代码里写明了这个形状的来源
（`src/aiops_diagnostics/identity/scope_context.py`）：

> 顶层账号的 `sys_user.type` 取值：`-1` 平台、**`1` 租户主账号**。公司自己的店铺隔离门
> （`ShopIdInterceptor.judge()`）对这两个类型**不做店铺隔离**（店铺头为空或 `-1` 时直接 return），
> 因此它们的可见范围是**整个租户**，数据上表现为「没有店铺绑定」——那不是
> 「看不到任何订单」，而是「不受站点维度约束」。

本次 `scope.caller.user_type=1`，与这条一致。租户边界另由 `scope_where_sql("tenant_id", …)`
渲染的租户谓词保证（`src/aiops_diagnostics/data/sources.py`），与站点过滤是两处独立定义。
所以本项结论是：**站点维度不受约束、范围等于整个 Ulink 租户**——不是跨租户。

### 4.2 真实订单反例（同一员工身份）

`ch_order_info` 里 Ulink 租户有 237 张订单；另取**另一个租户**的一张订单做反例。

| 用例 | 结果 |
| --- | --- |
| 本租户订单（脱敏） | **200 `completed`**，正文给出订单号、状态「充电结束」、电费/服务费/合计、电量、停止原因 `Remote`。`agent_runs.state=succeeded`，同一 operator 绑定 rev 12 |
| **另一租户**订单（脱敏） | **403** `CPMS_INVALID_RESPONSE`「operator request unavailable」 |

外租户订单被拒，说明"公司账号能登录"不等于"能读任意租户的订单"。

### 4.3 伪造租户头无效

在页面上下文里给同一请求加 `tenant-id: <另一租户>`、`site: <同一值>`，
服务端 `agent_runs` 记录的仍是 `effective_tenant_id=1942105476598861824`、
`chat_binding=…:1942105476598861824`。租户取自令牌，不取自请求头。

## 五、一条必须记下的坑：管家主机的 nginx 会改写入口头

同一页面上，无论 JS 传 `X-Business-Entry: consumer` 还是 `operator`，网关日志里都是
`reason=operator_entry`。原因在 41 的站点配置：

```nginx
# /etc/aiops-41/operator-h5-proxy.inc
proxy_set_header X-Business-Entry operator;
proxy_set_header X-Third-Session "";
proxy_set_header X-AIOps-Source-Key "";
```

**管家主机把入口头硬写成 `operator` 并清空第三方会话头**。所以：

- 在管家 H5 上做"把消费者头塞进员工端"的伪造就**不是一条可执行的测试**——请求还没出
  浏览器就被改写。要测得从别的入口打网关。
- 反过来，这也说明"员工令牌 + consumer 头仍然返回 202"不代表网关放过它；是 nginx 先把
  头改成了 `operator`。只读网关响应会得出错误结论，**要看日志里的 `reason=` 或站点配置**。

（同一域名只代理 `/v1/` 的白名单方法，见
`/www/server/panel/vhost/nginx/extension/ulinkmanage.h5.mall.qushiyun.com/aiops-assistant.conf`：
GET 只放 `faq/recommendations|catalog`、`shortcuts`、`assistant/questions/qa_<32hex>`，
POST 只放 `faq/answer`、`assistant/questions(/cancel)`。）

## 六、结论

| 子项 | 结论 | 判据 |
| --- | --- | --- |
| 员工端真实登录 | **通过** | `/upms/token/login` 200，`CLOUD_ACCESS_TOKEN` 写入，首页正文可见 |
| 员工端助手真实会话 | **通过** | `qa_959e0140…` completed，`chat_binding` = 该租户 operator 绑定，rev 12 |
| 跨租户订单 | **通过（被拒）** | 外租户订单 403 `CPMS_INVALID_RESPONSE` |
| 伪造租户头 | **通过（无效）** | 服务端记录的租户仍取自令牌 |

### 但 I 项整体判 **部分**，不是通过

验收规则 I（[`acceptance.feature`](../acceptance/dual-entry/acceptance.feature)）要的是
**管家遵守站点范围**，两个场景：

- **I1 空站点范围提示清楚**：员工账号未分配任何可诊断场站 → 页面明确说明未分配场站、
  且不创建执行任务、不读取订单数据；
- **I2 有权限订单可完成**：员工账号有该站点权限 → 提交该站点订单返回完整诊断。

本轮用的是 `data_scope.type=all` 的**租户主账号**（`user_type=1`），它同时能看到
整个租户的所有站点。所以：

- I2 只能算"租户内订单能完成"，**不能**算"站点权限生效"——没有站点被限制，就没有东西可验证；
- **I1 完全没做**：需要一个空站点的员工账号，本轮没有；
- "只拥有站点 A 的员工提交站点 B 的订单会被拒"这条反例同样无法构造。

这条边界初版写漏了，PR 复核（Devin）指出后订正：**本节证明的是入口与会话打通，不是站点范围**。
要闭合 I，需要一个**受限站点**的员工账号（库外资产里目前只有 `ulink` 与 `chongdian`
两个租户主账号），以及一个**未分配站点**的员工账号。

**其余边界**：只覆盖 Ulink 一个租户的员工端，优链充员工端未验；`ch_order_info` 的历史数据
不代表长期样本；`retrieval_status=unavailable` 是订单分支的正常值（未走知识库），
不应当作检索失败。
