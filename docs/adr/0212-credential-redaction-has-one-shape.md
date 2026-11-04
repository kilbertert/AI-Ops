# 212. 「什么算凭据」两种形状一个答案，URL userinfo 走 urlsplit

日期：2026-11-04
状态：已采纳

`redaction.py` 是本仓脱敏的单一落点，但「什么算凭据」在它内部按**数据形状**分叉：
映射的键走 `SENSITIVE_KEY.search`，自由文本走 `CREDENTIAL_ASSIGNMENT`。后者的 `\b`
在 `_` 处不成立（`_` 是单词字符），于是 `inside_token=` / `service_token=` /
`access_token=` / `client_secret=` / `integration_api_key=` 在自由文本里原样留下，
而同一个值只要躺在同名 JSON 键下就会被抹掉。可达落点是提问落库、知识正文、
证据 payload —— 都不是构造出来的路径。

URL userinfo 另有第二套实现（`config._redact_url_credentials`），对**任何 scheme**
去 userinfo，而 `redact_text` 只认 `https?://`；同一条 `redis://u:p@h` 连接串在
`--show-config` 里被抹掉、在被持久化的日志里原样留下。

## 决策与依据

- **名字用整段标识符匹配，不靠 `\b`**：`(?<![A-Za-z0-9_])` 后接名字集合，
  `xn_secret` / `x_token` / `integration_api_key` 同一答案。
- **URL userinfo 只有一个定义**：`redact_url_credentials(url)`，走 `urlsplit`，
  任何 scheme。`redact_text` 用一条粗筛正则找出「值得解析一下」的片段再委托它；
  `config._redact_url_credentials` 变成一行薄壳。粗筛只负责找，规则不在两处。
- **`signature_key` 判为凭据**：它是 `CompanyTokenSettings` 的本地验签密钥
  （`field(repr=False)`），此前只靠 `repr=False` 保护。
- **截断上限与标记只在一处**：`TRUNCATION_MARKER` / `ERROR_TEXT_LIMIT` /
  `ERROR_TEXT_SHORT_LIMIT` / `truncate()`，调用点不再写 `[:1000]` / `[:500]`。

## 备选方案

- **只放宽自由文本正则，不合并 URL 实现。** 被否：同一条连接串在两个面得到相反
  答案，是这份 PRD 的一半；只修名字那一半会留下它。
- **维持 `https?` 的 URL 白名单。** 被否：`redis` / `mysql` / `amqp` 的连接串同样
  携带口令，而「哪些 scheme 算」没有依据。
- **`preserve` 策略（把订单号留在提问里）本轮不动。** 它是一个产品决策，不是
  重复实现；#606 的 Further Notes 已记录它需要单独确认。
