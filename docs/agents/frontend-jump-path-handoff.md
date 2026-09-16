# 快捷动作跳转：前端联调交接说明

> **读者**：前端组。
> **状态**：后端已上线并完成 41 公网验收（2026-09-16，main `b0b3856` / 部署 `d2c35f5`）。
> **一句话**：`GET /v1/shortcuts` 的每条动作新增 `jump_path`；**有值就本地跳转，没值就走原来的提问流程**。

---

## 1. 现在线上真实返回什么（可直接照这个联调）

`GET /v1/shortcuts`，41 公网实测（2026-09-16）：

```json
{
  "type": "shortcut_list",
  "language": "zh",
  "count": 4,
  "shortcuts": [
    {
      "code": "case_exploration",
      "intent": "case_exploration",
      "requires_order": false,
      "sort_order": 10,
      "label": "客户案例",
      "description": "查看充电运营标杆案例",
      "question_template": "我想看看客户案例",
      "target_agent_version": "agt_ed443cae10ac4ba78c81b9d1b43fd91d#v1",
      "jump_path": null
    },
    {
      "code": "solution_discovery",
      "intent": "solution_discovery",
      "requires_order": false,
      "sort_order": 15,
      "label": "行业方案",
      "description": "查看充电行业解决方案",
      "question_template": "给我看看行业解决方案",
      "target_agent_version": null,
      "jump_path": null
    },
    {
      "code": "smart_diagnosis",
      "intent": "order_issue",
      "requires_order": true,
      "sort_order": 20,
      "label": "智能检测",
      "description": "选择订单后自动诊断充电异常",
      "question_template": "帮我检测这个订单的充电异常",
      "target_agent_version": null,
      "jump_path": null
    },
    {
      "code": "report_fault",
      "intent": "report_fault",
      "requires_order": false,
      "sort_order": 30,
      "label": "故障上报",
      "description": "描述故障现象，由平台跟进处理",
      "question_template": "我要上报一个故障",
      "target_agent_version": null,
      "jump_path": "/charge/pages/faultReport/faultReportList"
    }
  ]
}
```

**注意 `report_fault` 已经有真实路径了**，不是空值占位——可以直接跑通跳转。

---

## 2. 怎么判断：一个字段，一个分支

**判别依据只有 `jump_path`。** 服务端**不下发**类型字段，也不要按 `code`、`intent` 或 `label` 去猜：

```js
switch (item.jump_path) {
  case null:                 // 提示动作 —— 走原来的提问流程（见 §3）
    submitToAssistant(item)
    break
  default:                   // 跳转动作 —— 本地导航
    uni.navigateTo({ url: item.jump_path })
}
```

- **该字段恒存在**，非跳转动作是 `null`（**不是缺失**）。所以用 `item.jump_path` 的真值判断即可，不需要判 `undefined`。
- 服务端**不会**下发 `jump_path` 为 `""`、`"  "` 或 `"charge/..."` 这类值：格式在写入时就已校验。
- **不要**因为"怕漏"而在跳转动作上**仍然调一次统一助手入口**。那会拿到一个兜底澄清（见 §4），既多一次请求又得不到答案。

---

## 3. 提示动作（`jump_path === null`）的行为不变

原样保留，确认一下没改：

- `requires_order: true`（如 `smart_diagnosis`）→ 先弹订单选择器，选完把订单号**放进 `question` 文本**或单独传 `order_no`，两者等价；
- 其它 → 用 `question_template` 或用户输入，连同 `shortcut_code` 提交到 `POST /v1/assistant/questions`；
- 响应 `type` 仍为 `faq` / `clarification` / `qa` / `diagnosis`，按原逻辑处理。

---

## 4. 跳转动作没有响应 `type`

**跳转动作不经过统一助手入口，因此没有响应 `type`。** 别去响应类型表（`faq`/`clarification`/`qa`/`diagnosis`）里找它。

如果你**确实**把一个跳转动作投递到了 `POST /v1/assistant/questions`（比如旧版本客户端），服务端会兜底：

```json
{
  "type": "clarification",
  "missing_fields": [],
  "message": "请点击页面上的快捷按钮进入对应页面。"
}
```

`missing_fields` 为空数组表示"不缺字段，是走错入口了"。**这只是兜底，不是正常流程**，正常流程里前端应该已经本地导航了。

---

## 5. 几个容易踩的点

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | 把 `jump_path: null` 当成"接口还没实现" | `null` 是**提示动作的正常值**；字段一直存在 |
| 2 | 拿 `question_template` 给跳转动作预填输入框 | **跳转动作必须忽略 `question_template`**。产品规则是两者互斥（面板文案：「填写路径链接后用户点击将导航到对应界面，预设提示词不起作用」），但服务端仍会返回该字段的历史值 |
| 3 | 对路径做拼接、改域名 | **原样传给 `uni.navigateTo`**。路径由服务端下发，不做国际化（各语言同一路径）。若路径里带 `?k=v` 查询串，`navigateTo` 本身支持，照传即可——关键是别自己改 |
| 4 | 用 `uni.navigateTo` 跳 tabBar 页面 | 官方文档明确：**`navigateTo`/`redirectTo` 只能打开非 tabBar 页面**，tabBar 页必须用 `uni.switchTab`，否则失败。`/charge/pages/faultReport/faultReportList` 是否为 tabBar 页请前端确认 |
| 5 | （承上）以为 `switchTab` 也能带参数 | 官方文档：`switchTab` 的 **`url` 后不能带参数**。所以"tabBar 页 + 带查询串"这个组合在 uni-app 里无法直接实现，需要产品侧重新设计 |
| 6 | 跳转失败时静默无反应 | 建议在 `navigateTo` 的 `fail` 回调里给提示或降级为提问，避免用户点了没反应 |
| 7 | 在跳转动作上仍然提交一次统一入口 | 见 §2 / §4，不要 |
| 8 | 想拿它跳外部网站 | `navigateTo` **不能打开任意外部 URL**，只能打开 `pages.json` 里注册的页面。这也正是服务端把它定义为"站内路由"而不是"网址"的原因 |

> 框架行为依据：uni-app 官方路由文档 <https://uniapp.dcloud.net.cn/api/router.html>（`navigateTo` 只能打开非 tabBar 页面；`switchTab` 路径后不能带参数）。

---

## 6. 需要前端确认的一件事（重要）

**后端不校验页面是否存在。** 服务端只保证三件事：字段如实下发、格式合法（`/` 开头、不是 `//`、≤512 字符）、无路径时为 `null`。

补充一点：**服务端要求路径以 `/` 开头，这比 uni-app 更严格**——框架自身两种写法都接受（`'pages/test'` 与 `'/pages/test'`）。我们统一要求 `/` 开头是为了让下发的值无歧义，不影响你直接使用。

`/charge/pages/faultReport/faultReportList` 是**产品给定的默认路径**（Axure 原型「智能体编排」页的「跳转链接」字段值）。**但它是否是你这边真实存在的 uni-app 路由、能否正常打开，需要你们确认。** 仓库里没有任何 H5 路由约定文档，所以这不在我们的验证范围内——我们在验收记录里也如实标注为"未验"。

**如果实际路径不同：告诉我们改配置即可，不需要改代码。** 路径是数据不是代码——这正是把它做成服务端字段的原因（新增页面不必等 APK 发版）。

---

## 7. 相关文档

- 完整接口契约：`docs/agents/frontend-api-brief.md` 场景 D（D.1 列表字段、D.2a 跳转动作、D.4 响应类型速查）。
- 后端设计与取舍理由：`docs/adr/0006-shortcut-actions-extend-to-in-app-navigation.md`。
- 真实验收证据：`docs/validation.md`「快捷动作跳转路径：41 部署与公网真实验收（2026-09-16）」。
