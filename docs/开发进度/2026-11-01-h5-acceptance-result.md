# 原 H5 验收：登录通过，但 AI 入口不走提示词链路

用库外受控验收账号在原 H5 页面完成真实登录（发码 → 内存读当次验证码 → 真实会话），
这是通过的部分。

但点进 `My → Ai Trendi` 提问后复核服务端：`assistant_questions` 无新记录、
`chat_dify_runs` 最新早于本次提问；捕获的唯一请求是 `POST /v1/faq/answer`，
该端点是 `faq_catalog.answer(...)`，**只查本地 FAQ 目录，不经 Dify、不调模型、不消费提示词**。

因此**原 H5 不是本需求的有效验收面**。此前把"原 H5 六项通过"当作本需求验收项是错的——
那是双端开通任务的 H5 正例。

本需求真正的生产入口 `POST /v1/assistant/questions` 已于 2026-11-01 07:20/07:30
用真实用户会话验证通过（`fact_contract=slots`、`found`、来源 `KB-C-007.md`）。

仍未完成：订单/账单实时链路在正式入口未验（缺可见订单的真实会话）。**业务验收未完成。**

详见 [validation](../validation/2026-11-01-h5-acceptance-result.md)。
