# 当前交付状态索引

> **用途**：回答“现在是否完成”“41 能否联调”“快捷动作/意图路由是否已验收”以及变更
> 41 前，先读本页；再按链接查看完整证据。本文只保留当前可行动结论，历史过程在
> `docs/开发进度.md`，逐项证据在 `docs/validation.md`。
>
> **最后核验**：2026-09-22 20:36（Asia/Shanghai）
> **仓库基线**：读取时在 canonical checkout 执行 `git rev-parse origin/main`；最近固化的
> 状态索引变更为 PR #382（承接 PR #380）。

## 一分钟结论

### 当前进行中的 #247

迁移命令已由 PR #251（`1f708888`）合并并部署 41：支持 `--dry-run`、幂等平台默认创建和失败
草稿清理；原租户发布版本不删除。真实 41 已完成精确 SQLite 备份、迁移、幂等复跑和隔离恢复；
公网真实 H5 consumer 列表 200/4。operator 唯一 B 端身份、无覆盖新租户、停用和双租户动作执行
仍待补跑。任何本地 fixture 或 SQLite 检查都不能替代这些业务验收。

| 交付项 | 当前状态 | 证据/边界 |
|---|---|---|
| 意图路由 | 判定源已切到 Jev（未部署 41） | 寒暄/FAQ/订单诊断/缺订单号澄清/宣传路由见 `validation.md` 的 INTENT-01..08；`smart_diagnosis` 缺订单服务端保护已由 PR #253 修复并公网复测。`今天天气怎么样` 的 FAQ 关键词假阳性仍是独立遗留。**2026-09-23（PRD #383）：判定三字段改由 Jev 类型化判定产出**（`docs/validation.md` 的 #392 节）；阈值来自 #390 标定且可配置；Jev 未配置时仍走原模型分类器。**41 尚未部署**，公网复验待部署。 |
| 快捷动作 API | 平台迁移已部署，真实验收部分完成 | `GET /v1/shortcuts` 按会话租户和透传的 `business_entry` 返回有效结果；Nginx 入口硬编码已修复。consumer 200/4 通过，operator 正向主体、无覆盖新租户、停用和宣传/诊断成功路径仍待业务条件。 |
| 1942105476598861824/consumer | 已有 4 条已发布动作 | `case_exploration`、`solution_discovery`、`smart_diagnosis`、`report_fault`；前两者中的 `case_exploration` 已绑定宣传 Agent 版本。 |
| 当前前端会话演示 | 已补齐 | 2026-09-15 发现某 H5 `third-session` 实际解析为租户 `1899282205965029376`，请求头 `tenant-id` 不能覆盖该身份；已在其 `consumer` 入口创建并发布同样 4 条动作，公网复测 HTTP 200、`count=4`。 |
| 宣传内容点击验收 | 仅 1942105476598861824 有真实素材闭环 | 当前 H5 演示租户的 4 条动作仅用于列表与入口流程，未配置本租户宣传 Agent/知识库；不要宣称其客户案例卡片已完成真实内容验收。**2026-09-18 补充**：管理后台知识库页已归并到与 AI-Ops 同一套 36（见 `kb-service-test-env.md` §0），打通已就绪；但上述租户缺口未变——H5 演示租户 `1899282205965029376` 与素材所属的 `1942105476598861824` 均**不是 UPMS 租户**，后台租户切换器选不到，故其知识库只能在 API/智能体侧使用。真正**可在后台页管理、且已绑在跑智能体**的样本是 UPMS 真实租户 **`1783022023241633792`（小趋充电）** 的 `canary-media-0911`（绑 `canary-客服` v5）。 |
| 故障上报 | 仅信息收集 | 当前是 QA 完成态收集故障类型/桩号/时间，不创建工单，符合第一版只读边界。 |
| 流式轮次首部丢失 | 已修复并部署 41（2026-09-22） | PR #380 合并后按 runbook §2 部署（备份 + 逐文件 sha 核对，六个文件与本地逐一相同 → 重启健康）。公网用**当初失败的原问题**复验：`202 → running → completed` 且带完整回答，修复前稳定报 `invalid JSON`。**上游仍丢 delta**（`ai-api.baoyun.com` 直连复现 5/6）——本次交付的是客户端容错，未根治，长期需与上游沟通或改走非流式。证据见 `validation.md`。 |

## 41 复核与处理

41 网关服务为 `aiops-gateway-41.service`，公网正式路径是
`https://api.mall.qushiyun.com/v1/*`。`/aiops/v1/*` 不是该入口，2026-09-15 实测为
404。快捷动作数据位于 41 Gateway 的 SQLite 库，由 `ShortcutManager` 管理；不要直接
插入 `shortcuts` 表。**完整的访问、部署、资源创建与验收步骤见
[41 环境运维与真实验收手册](env-41-runbook.md)（本页只保留当前结论，不重复操作细节）。**

### 安全复测

使用业务方提供的有效会话，保留 BFF 入口头；不要记录或提交 `third-session`：

```bash
curl --silent --show-error --max-time 20 \
  -H 'Accept: application/json' \
  -H 'Accept-Language: zh-CN' \
  -H 'X-Business-Entry: consumer' \
  -H 'tenant-id: <前端声明租户，仅供 BFF 兼容>' \
  -H 'third-session: <当前有效会话>' \
  'https://api.mall.qushiyun.com/v1/shortcuts' \
  | jq '{type, language, count, codes: [.shortcuts[].code]}'
```

预期是 `type=shortcut_list`，并且动作只反映**会话实际租户**。`count=0` 时按以下顺序
排查：会话解析租户 → `X-Business-Entry` 最终决策 → 该身份下是否存在 published 行；不要
用请求头 `tenant-id` 推断数据应该属于谁。

### 资源变更守则

1. 先只读列出目标 `tenant + business_entry` 的 action/status/revision，并确认运行服务使用的数据库。
2. 对精确 SQLite 文件做可恢复备份。
3. 用 `ShortcutManager` 的 create/publish 生命周期创建；发布后再用公网 `GET /v1/shortcuts` 验收。
4. 快捷动作本身不授权创建订单、工单、退款或修改配置；这些动作保持只读诊断/问答边界。

## 证据导航

- [41 环境运维与真实验收手册](env-41-runbook.md) — 部署/资源创建/会话获取/公网验收的可执行步骤
- [真实 41 INTENT-01..08 与模型契约修复](../validation.md)
- [助手入口等待态与取消：BFF / 前端交接契约](assistant-cancel-handoff.md) — 输入框锁定/复原规则、取消端点契约、`cancelled` 终态、真实响应样例（PRD #346）
- [里程碑、分支和已知缺口](../开发进度.md)
- [前端快捷动作与 clarification 合同](frontend-api-brief.md)
- [验收场景与人工复跑步骤](../../qa-plan.md)
- [可执行验收约束](../../acceptance.feature)

更新本页时必须同步更新验证证据；没有公网或集成实测时，状态写“待验证”，不要把
fixture、数据库检查或模型单次调用写成真实业务验收。
