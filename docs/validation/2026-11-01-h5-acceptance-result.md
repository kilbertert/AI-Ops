# 41 原 H5 验收：登录成功，但 AI 入口不走提示词链路（2026-11-01）

## 做到了什么

用库外受控验收账号 `aiops-ulink-phone-20261030`（Ulink 租户 `1942105476598861824`）
在**原 H5 页面**完成真实登录：

1. `https://ulink.h5.mall.qushiyun.com/shopPackage/pages/login/index`
2. 填手机号 → 勾选隐私协议（`.agree_circle`）→ Next → 页面发码
   （"Code sent. Resend in 56 seconds"）
3. 从受控 Redis `ver_code_default::13333555555` **在内存读取当次验证码**（TTL 281s），
   填回同一页面提交。**未打印、未写入 Redis、未使用固定码 8888**
4. 登录成功，进入 H5 首页（站点地图 + Orders/My 等真实业务页）

## 关键发现：H5 的 AI 回复不走提示词链路

进入 `My → Ai Trendi`（`/aiPackage/pages/chat/chat`）提问后，页面显示了英文回复。
但服务端**没有对应记录**：

```
assistant_questions（07:50 之后）: 0
chat_dify_runs 最新: 07:30:06（比本次提问更早）
```

捕获的网络请求只有一条：

```
POST /v1/faq/answer  200
```

而 `/v1/faq/answer` 的实现是 `context.faq_catalog.answer(...)` ——
**只查本地 FAQ 目录，完全不经过 Dify、不调用模型、不消费提示词**
（`gateway_api.py:2718`）。

### 含义

本轮提示词生命周期改动（slots 契约、Dify 提示词、事实片段）**不在这条 H5 路径上生效**。
H5 里看到的 AI 回复是 FAQ 目录的确定性文案，不是模型输出。

**因此"41 原 H5 验收"对本需求不成立**：它不是本功能的客户端入口。

## 修正一个此前回执的说法

此前的回执把"原 H5 六项通过"当作本需求的验收项之一。按本次实测，
原 H5 的 AI 助手走 FAQ 直答，与 Dify 提示词无关。那条记录说的是**双端开通**任务的
H5 正例，不是本需求的 H5 验收。

## 本需求真正的生产入口

`POST /v1/assistant/questions` —— 已在 2026-11-01 07:20 与 07:30 用真实用户会话验证：

```
fact_contract=slots  status=found  sources=5  来源 KB-C-007.md
```

## 当前状态

- 原 H5 登录：**通过**（真实账号、真实发码、真实会话）
- 原 H5 的 AI 链路：**与本需求无关**（FAQ 直答）
- 本需求生产链路：**已通过**（`/v1/assistant/questions` + slots）
- 订单/账单实时链路：**正式入口未验**（缺可见订单的真实会话）

**业务验收未完成。**
