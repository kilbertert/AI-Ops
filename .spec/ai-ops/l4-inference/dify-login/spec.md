---
title: dify-login
desc: 用公司账号换 Dify 会话——身份结论只来自公司登录交换
hue: 60
code:
  - src/aiops_diagnostics/dify_login_proxy.py
related:
  - src/aiops_diagnostics/dify_login_server.py
  - src/aiops_diagnostics/dify_login_service.py
  - src/aiops_diagnostics/dify_login_company.py
  - src/aiops_diagnostics/dify_login_provision.py
---
# dify-login

它替代的东西很具体：运营今天要拿一个**与公司身份无关**的 Dify 邮箱密码登控制台——
公司里谁该改智能体、谁该发布到生产，Dify 都不知道；人员离职也不会反映到 Dify 账号上。
这一层把「谁是这个人」重新交给公司（ADR-0023）。

## raw source

**身份结论只来自公司登录交换。** 请求里的任何字段都不参与"这是谁"的判定。
会话是**自签**的 HS256 JWT——因为 Dify 1.17.1 的控制台会话本就是它自己的 `SECRET_KEY` 签的，
所以"自签"不是绕过认证，是按它自己的规则签发。

## 不变量

- **TLS 未就绪则拒绝服务**：公司密码不得经明文 HTTP 传输。就绪判据是配置字符串是否以
  `https://` 开头（与 Dify 自身 `is_secure()` 的判据一致——看配置，不看请求）；
  未就绪时**启动即失败**，不留一个"能跑但危险"的状态。
- 映射表的**键是公司用户 id**，不是邮箱：同一公司用户二次登录必须落到同一个 Dify 账号。
  **表里没有密码**——自签会话模型下不存在"要保管的密码"。
- 开账号走官方邀请接口、**全程不经过邮件**；不用自带注册（`ALLOW_REGISTER` 是 PRD 明确要
  保持关闭的），也不用权力过宽的共享静态密钥。
- 公司 API 的四个形状事实是**源码核实**的，不是猜的：登录体是表单不是 JSON；密码要 AES；
  登录失败是 HTTP 200 + `code:-1000`；失败只有中文 `msg`。
- 每一步失败必须区分「凭据被拒」与「服务不可用」——代理要对运营说清下一步做什么。
