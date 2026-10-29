# 优链充租户自主管理真实验收

实现#1147部署`b86be95f`，修复#1150合并`f58e86d62729b23236fef4138eb0e79ece86beae`，
CD `40840154559`成功；桥同步安装该修订。前端镜像`aiops/dify-web-patched:self-service-f58e86d6`，
ID `sha256:beec2fcb73e42a20e8e8c20e4971fb5fcd01031d49ff08274d02c90ad5548f74`。
修复CI3349 passed/10 skipped，本地3350 passed/9 skipped、专项60、Chromium4、临时Gateway8项通过。
自动完整审查因额度不足跳过，无待解决审查线程。

## 正常账号与页面

用户授权代操作的优链充公司账号正常密码＋图形验证码登录，Dify进入其workspace。
管理API返回platform=false；readiness=true。不能将租户账号当成平台跨客户开通的正例。
同账号请求另一公司开通返回403 SETUP_PLATFORM_PERMISSION_REQUIRED，未创建任务。

原生Duplicate创建“优链充－自助开通验收”，app `65255300-e932-4589-bf06-4bb8ba7598cd`。
副本自动登记为受管草稿，页面显示尚未发布，保留双CPMS工具。它没有替换正式V2/revision1。
同账号在原应用点击“发布到管家”，保存与发布成功；审计2026-10-29T08:15:04Z/版本2/published，
正式版本和revision未增，证明相同内容重复发布不会制造新版本。

## 真实预览与失败

从该账号正常公司登录后的订单列表选择明确订单，仅执行只读诊断。
第一类失败发生在桥：Dify附带空上下文字段被拒绝，Java未派发。#1150修复并部署后，该错误消失。
之后首个Java运行qa_ad8dabd278654e189aea3a617004d50f仍在完成阶段失败，state=failed、reason=operator_failed；
双CPMS已读取，exec_config_state=expected。保留真实失败，不将其掩盖为成功。
随后同订单qa_fa6f98581fd34bf0832f728dccda4ee6成功：run_UbP1cGYK6inDg1plxmOizpI9VAOmbX_W4C2I1ihAv9k，
state=succeeded、exec_config_state=matched，chat_binding=operator-trial:65255300-e932-4589-bf06-4bb8ba7598cd，
snapshot.trial=true，双CPMS证据。浏览器确实显示完整“Java 节点试用”报告，说明0条采样缺证。

原4条正式路由保持不变。用户未要求把验收副本替换正式管家，因此只保留草稿。
预算文案修复说明Java未声明max_tokens时使用模型默认，不能套用Dify4096凭据字段。
最终登录会话自然到期时页面提示重登；随后按正常流程重新登录，已确认“输出上限：模型默认”、正式V2可见，页面错误0。

**租户内复制、未发布草稿真实预览、重复发布与跨租户拒绝已验。平台跨客户开通仍未完成业务验收**，
需要公司平台身份选择优链充为目标。Java历史间歇完成失败仍独立跟进，另一个#1149修复的效果不由本回执代验。
原始截图、SSE与运行摘要仅保存在私有`~/.cache/aiops-dify-self-service-research/tenant-*`，无凭据入仓。
