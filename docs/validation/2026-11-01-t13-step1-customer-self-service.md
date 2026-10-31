# T13 第 1 项：客户自主管理闭环（真实环境）

执行时间：2026-11-01（UTC 01:1x–01:5x）。环境：41 生产网关、36 控制台、Ulink 租户。

## 执行身份

客户运营账号 `ulink`（资产 `aiops-ulink-account-20261030`，经 Dify 登录代理正常登录，
`account/profile` 返回 `name=ulink`）。该账号 `/console/api/workspaces` **只看到自己那一个**
工作区 `aiops-41-Ulink-598861824` —— 这是"客户在自己的空间里操作、看不到别人"的前提。

平台账号仅用于对照读取，不参与任何客户侧写操作。

## 逐项结果

| 步骤 | 结果 | 证据 |
| --- | --- | --- |
| 1. 改提示词并保存 | **通过** | `POST /console/api/apps/<id>/model-config` 200；草稿 `pre_prompt` 183 → 196 字符 |
| 2. 保存不影响正式版 | **通过** | 同一次读取里 `published_prompt` 仍 183 字符、不含草稿标记；路由 rev 5 未动 |
| 3. 草稿预览 | **通过** | `POST /__aiops/trial` 202 → 轮询 `/__aiops/trial/result` → `completed`；回答复述 `DRAFT-MARK-7Q3`，证明执行的是**草稿**而非正式版 |
| 4. 发布 | **通过** | `POST /__aiops/manage {action:publish}` 200，`version_no=2`，路由 rev 6 |
| 5. 原 H5 验证 | **未完成** | 见下 |
| 6. 撤回 | **通过** | `action=retract` → `published=false`，状态回读 rev 7 |
| 7. 恢复 | **通过** | `action=restore {version_no:2}` → 200「已发布到线上（版本 2）」，rev 8，内容仍是客户改过的那份（232 字符） |

## 隔离性（"改一个客户/一端不影响另一个"）

发布 Ulink 管家端 v2 之后读全部路由与版本快照：

```
优链充 operator  v1  a57602c1  active rev2    ← 未变
Ulink  consumer  v1  07d15c42  active rev9    ← 未变
Ulink  consumer  v18 88fc20bd  active rev1    ← 未变（现役原客服）
Ulink  operator  v2  2b0629cf  active rev8    ← 本次操作
```

内容层面：`DRAFT-MARK-7Q3` 只出现在 Ulink operator v2 的快照里；优链充 operator 的 v1/v2
与 Ulink consumer v1 快照里都**没有**这个标记。

## 一个操作细节值得记

草稿预览结果的保留期很短（该次运行的 `expires_at` 比提交时间晚 3 分钟）。第一次读是
`410 TRIAL_RESULT_UNAVAILABLE`，不是缺陷；**提交后必须立即轮询**，隔几分钟再读就过期了。

另外，Agent 模式应用的发布请求体需要 `revision` + `draft`（网关的
`draft_fingerprint(model_config)`），桥不会替你算。

## 未完成：原 H5 验证

按计划第 5 步应在原 H5 上验证客户改的提示词真的进了线上。实际卡在**公司商城后端**：

- 原 H5 登录页可正常打开、发码接口可调；
- 但 `/mallapi/userinfo/phone/login` 返回 `System error`，`cloud-mall-api` 日志为
  `NullPointerException: ThirdSession.setUserId(...) because "thirdSession" is null`；
- 该次请求的 `user_info` 查询返回 `Total: 1`（用户存在，不是注册路径），说明失败发生在
  登录成功后的会话写入环节。

这是商城后端的缺陷，不在本仓范围。**因此"H5 上看到新提示词"这一条本轮没有证据，
不记为通过。** 已通过的是"发布动作本身成功、路由与版本快照内容正确"。

## 结论

客户运营**不需要 SSH、不需要手填技术 ID、不需要重启**即可完成：改提示词 → 保存 →
预览 → 发布 → 撤回 → 恢复。这一步的目标已经达到。

缺的是**发布后真实用户在 H5 上看到的确认**，阻塞于外部系统。
