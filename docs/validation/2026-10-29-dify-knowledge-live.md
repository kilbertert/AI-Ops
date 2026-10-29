# 平台开通与优链充知识空间全链路验收

日期：2026-10-29（北京时间）。本轮修复跨越#1155、#1158、#1164；不同环节分别核验，不以合成测试代替生产。

## 根因与修复

1. 自助开通只准备Dify，未调用已有RAGFlow开户。#1155 `1be3f067`将知识空间/模型/凭据确认纳入完成条件，旧任务可重试补齐。
2. kb-service曾懒注册优链充但没有Gateway密码。保留原RAGFlow用户与token，按受控工具恢复凭据；#1158补正RAGFlow实际密码形状。现场确认model_checked_at为2026-10-29T10:41:44Z。
3. 知识同步还复用了Ulink工作区API模板UUID，优链充没有自己的连接。#1164 `88c3887c`按当前工作区创建/回读连接，开户和同步共用；旧有效连接优先直读，避开无关归档坏密文。

## 制品和自动检查

- #1155本地3382 passed/9 skipped；前置知识流程88专项、Chromium5、临时Gateway8项，定向变异4项检出。
- #1164本地3403 passed/9 skipped，临时Gateway8项、连接专项和身份变异通过，模块75%语句/分支覆盖。
- 两PR必需CI通过；自动完整AI审查因额度耗尽未完成，未当作审查通过。
- Dify web `aiops/dify-web-patched:knowledge-1be3f067`，image ID `sha256:0ac260feb0f13c5458ccb1cdb669ceadd48c2beefbb6d71ee9ce1d45317d27ef`，传输摘要`40544c4426f4c6b0e91e687ab33a651c76c43186d0ae3a1cfb4a321855f7916b`。
- Gateway #1164 CD `40868968944`成功；后续#1163累计版本CD `40869662035`也成功，包含知识修复。
- 发布桥保留知识workspace授权和另一会话对话修复；不倒退后续合并版本。

## 真实平台到知识库

平台账号通过正常公司密码/图形验证码登录，platform=true，可选目录328客户。凭据按用户授权保存在
库外age资产`aiops-41-platform-dify`，已经加密往返验证，仓库只有使用方法。

| 步骤 | 真实结果 |
| --- | --- |
| 平台复制到优链充 | 操作`3cd45ef6-7b69-4ecc-828d-b0e80c39bd85`，app `a57602c1-6a8d-57cd-8ea0-6cd7fd9031bf` |
| 原任务重试补齐 | completed/knowledge_ready=true，相同workspace `9d2ccbac-5842-4b8a-8d02-36c5598528df`、app不变 |
| 页面进入工作区/知识库 | 平台可切Ulink/优链充；优链充知识入口303后正常进入RAGFlow，不再“尚未开户” |
| 原生RAGFlow建库上传 | 测试库`c8eec310d38a11f1a026f1f706dc4043`，265字节TXT，doc `2d1f47f6d38b11f1a026f1f706dc4043` |
| 解析 | DONE、progress=1、1切片；embedding与索引实际完成 |
| RAGFlow检索 | 1条命中测试标记`YLC-KB-20261029-7QX` |
| Dify同步 | 创建本工作区external dataset `3d8a7f04-9052-4737-a99e-9f51a3b706ff`，all_team_members |
| Dify原生Retrieval Test | 1 Retrieved Chunks，score 0.89，正文命中同一标记 |
| 跨工作区负例 | 切Ulink后列表无优链充库；直接读取该dataset返回403 |
| 清理 | 原生RAGFlow DELETE只删除本次标记测试库，success_count=1；Dify后续同步撤掉对应条目 |

空间、连接、开户任务保留，未把业务资料当作测试删除。知识库开通/解析/同步/检索链已验收。

## 当前状态与限制

最终路由回读：优链充正式入口现为平台验收app V1/revision2/active；发布审计时间为北京时间18:59:43。
这不是自动同步切版：同一期间发生显式发布，原任务资源记录仍保留最初published=false。
其他三条路线保持原值。两条该app试用run`qa_6fb4d682…`/`qa_d6cbe443…`（chat_binding
`operator-trial:a57602c1…`，exec_config_state=`matched`）在北京时间18:57/19:00均succeeded；
这是链路证明，不是本轮知识检索验收替代了诊断准确率验收。

**这次切版按用户决定保留（2026-10-29）**，不当成故障回滚：原正式应用`349a637c…`V2仍是
`agent_versions`中的不可变版本，路由切回只需在该应用页面走正常“发布到管家”，不需要改库。
切版期间该租户`chat_dify_runs`为空，路由上没有真实用户流量被切走。

**由此留下的清单漂移（尚未修，按本文档如实记录）**：`ops/environments/env-41.toml`仍把这个
租户的operator绑定声明为`349a637c…`／`ops-youlian-order-diagnosis`，而页面已把该
`(租户, operator)`登记为页面管理的`a57602c1…`／`operator-a57602c1…`。按`reconcile_dify_registry`
的守卫（“页面管理的管家不能由环境清单覆盖”），此时对该清单执行`aiops admin reconcile`会在
写入前中止。今日在41只读复算三条`[[dify_apps]]`：前两条`GUARD_RAISES=False`，该租户一条
`GUARD_RAISES=True`。这是守卫**按设计**拒绝覆盖页面选择，不是回归；但一次收敛会被整份清单
拖住。修法是让该租户的operator行归页面管理（删掉清单行，或同时对齐app_id与agent_name），
属于仓库配置与线上数据的独立变更，需单独验证，未在本轮改动。

开发机直连kb.mall域名超时，按既有41隧道保留域名/TLS/正常会话完成浏览器验收；41本机入口200。
公司会话15分钟窗口期间需要重登，未注入会话。CD被两条已合并PR的过期Agent Update Branch占用，
确认无待交付分支后取消过期更新任务，未取消必需CI或正在部署服务。

原始截图、API结果和任务快照保存在私有`~/.cache/aiops-dify-self-service-research/`。未完成长期稳定性、
任意文档解析质量或诊断根因准确率验收；本轮只证明平台与该租户的完整开户知识链及隔离。

普通优链充账号正常登录后也已成功进入本租户RAGFlow；跨workspace知识入口负例见最终回读。
