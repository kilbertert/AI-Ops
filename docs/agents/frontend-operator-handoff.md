# 管家端（operator）入口：前端联调交接说明

> **读者**：前端组 / BFF。
> **状态**：后端已上线并完成 41 公网验收（2026-09-28，main `056873d` / 41 运行 `0.1.0+67954175fca2`）。
> **一句话**：管家端与客户端**共用同一个助手入口**，靠请求头 `X-Business-Entry: operator`
> 切换；入口动作已发布，两个按钮点下去的行为见 §3。
> **先读**：[client-type-vs-business-entry.md](client-type-vs-business-entry.md) ——
> 为什么**不是**用 `client-type` 区分。

---

## 1. 与客户端唯一的差别：一个请求头

```http
# BFF → AI-Ops（服务身份由 BFF 注入，不下发前端）
Authorization: Bearer <aiops-service-token>
third-session: <当前用户的有效 thirdSession>   # 全小写连字符
tenant-id: <会话所属租户>
X-Business-Entry: operator                      # ← 管家端就是这一行
Content-Type: application/json
```

- 值只有两个：`consumer` / `operator`。**不带就按 `consumer` 处理**（41 的 Nginx 兜底）。
- 大小写与空格会被规范化，`Operator` / `" operator "` 等价于 `operator`。
- 非法值（如 `operator-admin`）会被拒：`403 PLATFORM_FORBIDDEN`。
- **双平台身份（既是 C 端用户、又有管家端角色）必须显式带这个头**，
  否则得到 `409 PLATFORM_AMBIGUOUS`（平台决策无法自行二选一）。

路径与其余头与客户端**完全一致**，没有第二个入口、没有第二套 URL：

```
POST https://api.mall.qushiyun.com/v1/assistant/questions
GET  https://api.mall.qushiyun.com/v1/shortcuts
```

## 2. 动作列表（直接照这个联调）

`GET /v1/shortcuts`，41 公网实测（2026-09-28，`X-Business-Entry: operator`）：

```json
{
  "type": "shortcut_list",
  "language": "zh",
  "count": 2,
  "shortcuts": [
    {
      "code": "case_exploration",
      "intent": "case_exploration",
      "requires_order": false,
      "sort_order": 10,
      "label": "客户案例",
      "description": "查看与充电运营相关的标杆案例",
      "question_template": "有哪些充电运营的客户案例",
      "target_agent_version": null,
      "jump_path": null
    },
    {
      "code": "smart_diagnosis",
      "intent": "order_issue",
      "requires_order": true,
      "sort_order": 20,
      "label": "订单检测",
      "description": "选择订单，检测该订单的充电异常",
      "question_template": "帮我检测这个订单的充电异常",
      "target_agent_version": null,
      "jump_path": null
    }
  ]
}
```

**怎么渲染**（与客户端同样的规则）：

| 字段 | 前端行为 |
|---|---|
| `jump_path` 有值 | **本地跳转**，不发请求（管家端这两个都是 `null`，所以都属下面的"提示动作"） |
| `jump_path` 为 `null` | **提示动作**：点击后带着 `code` 调助手入口（见 §3） |
| `requires_order: true` | 点击后**先弹订单选择器**，带上选中的订单号再发请求 |
| `target_agent_version` | 管家端两个动作都是 `null`，**不要**据此做任何绑定假设 |

> `question_template` 可直接预填输入框；`label`/`description` 已按 `Accept-Language` 本地化
> （当前支持 zh/en/de/fr/es/pt，缺失语言回退 zh）。

## 3. 两个动作点下去会发生什么（41 实测）

### 3.1 订单检测（`smart_diagnosis`，`requires_order: true`）

**不带订单号**（用户还没选单）：

```http
POST /v1/assistant/questions
{"question": "帮我检测这个订单的充电异常", "shortcut_code": "smart_diagnosis"}
```

→ **`200` `type=clarification`**，`missing_fields: ["order_no"]`，**不创建任何作业**。
前端此时弹订单选择器即可。

**带上订单号**（用户选完单）：

```http
POST /v1/assistant/questions
{"question": "帮我检测这个订单的充电异常", "shortcut_code": "smart_diagnosis", "order_no": "<订单号>"}
```

→ `202` `type=diagnosis` + `retry_after_ms`（轮询契约与客户端**完全相同**）。

**订单不在该用户可见范围内** → **`404` `ORDER_NOT_FOUND`**。
⚠️ **这与客户端的一个既有契约不同，前端要区别对待**：

- **显式带 `order_no`**（本条路径）：无权限 → **明确的 404**。前端应提示"订单不存在或无权查看"，
  **不要**把它当成网络错误重试。
- **问句文本里只是顺带提到订单号**：无权限时**静默回落**成普通问答（`200`/`202` `qa`），
  **不是** 404 —— 这是既有契约，保持不变
  （用例 `tests/test_assistant_api.py::test_assistant_text_embedded_unowned_order_falls_through`）。
- **会话里已确认的活跃订单**、追问时无权限：同样静默回落，并清掉该绑定。

### 3.2 客户案例（`case_exploration`，`requires_order: false`）

```http
POST /v1/assistant/questions
{"question": "有哪些充电运营的客户案例", "shortcut_code": "case_exploration"}
```

→ `202` `type=qa` → 轮询至 `completed`。

**当前真实终态（41 实测）**：`retrieval_status: "unavailable"`，
文案「客户案例服务暂时不可用，请稍后重试。」

⚠️ **这是预期行为，不是故障**：该动作的知识库素材**尚未配置**。
前端应把它渲染成「暂不可用」，**不要**渲染成"没有找到案例" ——
后端刻意区分了这两种状态（`unavailable` vs `not_found`），
把配置缺口说成"确实没有案例"会让用户与运维都找错方向。

**素材配好后**：同样的请求会返回 `retrieval_status: "found"` 与内容块，
前端**不需要**改代码。

## 4. 与客户端共用的部分（不要重复实现）

- **返回体形状、轮询、取消、会话**：与客户端**完全一致**。见
  [frontend-api-brief.md](frontend-api-brief.md) 与
  [assistant-cancel-handoff.md](assistant-cancel-handoff.md)。
- **会话（`conversation_id`）按入口隔离**：同一用户从 `operator` 与 `consumer`
  进来的会话是**两个**，`GET/DELETE /v1/conversations/{id}` 跨入口会得到 `404`。
  这是有意的，前端不要复用同一个 `conversation_id`。
  （`business_entry` 是会话绑定的组成部分；用例见 `tests/test_conversation_api.py`
  的"same user, different entry → 404"。）
- **可见订单范围不同**：管家端按"该账号所属运营商的站点集合"，客户端按"本人"。
  同一条订单，两个入口可能一个能查一个不能 —— 这是**预期**，不是数据不一致。

## 5. 前端需要做的（清单）

- [ ] 进管家端的页面带上 `X-Business-Entry: operator`（或由 BFF 按入口注入）。
- [ ] 动作列表按 `jump_path` / `requires_order` 两个字段决定点击行为，**不要**硬编码 `code` 列表。
- [ ] 订单检测：无订单号时弹选择器（用后端返回的 `missing_fields` 判，不要自己猜）。
- [ ] 显式带 `order_no` 得到 `404` 时，提示"订单不存在或无权查看"，**不重试**。
- [ ] 客户案例：`retrieval_status=unavailable` 渲染成"暂不可用"，不写成"没有案例"。
- [ ] 不要保存、打印或打包 AI-Ops 服务令牌（它只在 BFF 与 Nginx 之间）。
- [ ] 不要试图用 `client-type` 切换入口（见[另一篇](client-type-vs-business-entry.md)）。

## 6. 已知边界（后端如实告知）

- **「客户案例」暂无知识库素材**，恒为 `unavailable`（§3.2）。素材由产品提供。
- **入口头是调用方自报的**：41 的 Nginx 只做"没带就当 `consumer`"的兜底，
  不校验自报值。真实前端按入口发它即可；这不放大到"无范围"（管家端范围仍受运营商站点集合约束）。
- **管家端 App 的真实登录链路未端到端验收**：本次验收用的是"恰好有 B 端账号的会话 +
  显式 `operator` 头"，走的是**同一条生产判定路径**；差别只在"谁发那个头"。
  前端接入后请复跑 §3 的两个场景各一次。
