# 预览：需要的凭据与当前可做/不可做（2026-10-31）

## 试运行入口的真实要求

`POST /v1/dify/management/trial` 需要 **两个**凭据，缺一不可：

1. `Authorization: Bearer <AIOPS_GATEWAY_DIFY_TRIAL_API_KEY>` —— 已配置，我持有；
2. `X-AIOps-Console-Session: <公司控制台会话>` —— **没有**。

第二项由 `managed_trial_caller` 强制：

```python
session = request.headers.get("X-AIOps-Console-Session", "")
row = context.self_service.authorize_app(auth.identity(context, session), app_id)
caller, _ = authenticated_operator_console(context, session)  # ← 公司登录
```

`console_identity("")` 直接抛 `OPERATOR_TRIAL_LOGIN_REQUIRED`，
所以我的调用只能是 403。

**这是有意的设计**：这条路由触发一次**真实模型执行 + 真实数据访问**，
所以要求发起人是一个可核验的公司用户，而不是一把服务钥匙。

## 我为什么停在这里

伪造一个公司控制台会话等于绕过身份校验。任务书写明"不输出密钥、不取巧"，
我也不会为了"把预览跑出来"去构造一个假身份。

## 本轮实际取得的东西

1. **发现并订正了目标应用错误**：
   当前生效的 C 端绑定是 `07d15c42-…`（`chat_route_bindings` revision 9，10-31 12:15:19，
   `updated_by` 为公司用户），不是 `dify_app_registry` 里的 `394b5399-…`。
   我之前基于 `394b5399` 的全部取证**对象错了**。
2. **确认 `07d15c42` 的可迁移性**：14 节点、3 个环境项与 `394b5399` 同构；
   `fact_contract` 同样缺失；SYSTEM 906 字符、无协议块。
3. **推定冲突必然存在**：`07d15c42` 的 906 字符 SYSTEM 里没有任何事实片段占位，
   而 `documents` 模式要求 `reply` 只能引用 `{{fact:…}}`。迁移时同样需要
   先解决"旧 SYSTEM 要求照抄 vs 新协议禁止片段外数字"的冲突。
4. 授权、目录、kb-service、隧道均实测正常（`200 / 458 字节`、5 个库、active）。
   失败原因**不是**这些。

## 需要你决定

要给预览一个真实身份，只有两条路：

- **B1** 由你（或任一公司账号）在浏览器登录控制台，把 `X-AIOps-Console-Session`
  在一段时间内提供给我，我用它跑预览；
- **B2** 你在浏览器里自己操作「预览」按钮（就是产品本该有的交互），
  我在旁边读结果与证据。

两条都要真人参与，因为这道闸判的就是"人"。我无法在无人的情况下打开它。
