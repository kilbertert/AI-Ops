# 0155 — 管家原生预览复用 Java 节点试用授权

Status: Accepted

## 决策

原生 Dify 编辑器选择 cpms-node 的预览走现有控制台桥与 Gateway 试用，Java 节点继续
承担模型循环和 CPMS 取证。预览提交当前未保存的 pre_prompt 与工具选择，由 Gateway
按本租户实际 provider UUID、两项实现工具、固定输入 schema 验证后生成试用清单。
节点模型、端点和模型参数仍来自可信执行模板；浏览器不能提交公司令牌或扩大工具权限。

公司身份来自原登录代理的短期 Dify 会话交换；与正式入口使用同一单订单/站点校验。
首期每条消息必须有唯一明确订单，不猜“上一个订单”，不把前次会话订单自动用于新问题。
预览不发布、不改正式指针；正式路线按用户本次授权持续启用，仍可撤回/恢复。

## 备选方案

- 给 Dify provider 填长期公司 token：改动小，但所有预览共享身份，无法绑定本次人和订单，拒绝。
- 在 Dify 再跑一套模型授权循环：保留完整原生内部轨迹，但会与 Java 正式运行产生两套校验、
  取证和失败语义，且需把临时凭据注入工具层。本次复用已有节点执行，避免双实现。
- 只要求运营使用另一个试运行按钮：已有可用框架，但原生框仍会触发无授权工具调用，不能满足需求。

## 界面与失败语义

在既有同源注入脚本中，只接管包含 cpms-node 的 chat-messages POST；普通预览走原请求。
桥用 SSE 返回任务显示项、报告和 message_end；Agent 面板所需空 thought 仅是显示容器，
不声称是模型推理。结果历史保留于 Gateway QA/run，原预览为单轮，不伪造 Dify 会话历史。

停止按钮、超时和连接断开请求撤销本次 trial；Gateway 核对当前公司身份、任务归属及 trial
标记后复用既有取消逻辑，不可用此入口取消正式任务。回读再次核对当前授权和正式版本。
取数失败不回落共享账户、旧 Python 或无授权原生预览。

## 协议依据与代价

SSE 帧采用 HTML Living Standard 的 text/event-stream/data/空行格式；事件字段按当前
Dify 1.17.1 web/service/base.ts 和 chat/hooks.ts 的实际消费者核对。Origin/CSRF 复用
既有 RFC 6454 同源门，字段扩展维持拒绝额外输入。
注入依赖当前请求形状，升级 Dify 必须复跑浏览器请求/停止契约；不新增前端构建依赖。
编辑器模型设置暂不控制 Java 平台模型，这是已存在的节点发布边界，界面说明与手册需明确。

验收见 [场景](../agents/operator-cpms/native-preview.feature) 与
[QA计划](../agents/operator-cpms/native-preview-qa.md)。
