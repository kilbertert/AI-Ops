## 2026-10-23 · 这条链的验收判据挪到服务端（#1023）

**为什么挪**：这条链一直用浏览器验收，而浏览器有两处会让判据撒谎 —— 那天连着误判两次。

### 一、浏览器判据的三个坑（都是实测）

| 现象 | 真相 |
|---|---|
| 新标签 `tab.url` 变空串 | 303 指向 `kb.mall.qushiyun.com`（**另一个域**），跨域后读不到 URL。被读成"没跳转" |
| 同一个 URL，`fetch` 401、顶层导航 303 | `fetch` 与顶层导航的凭据行为不同 —— 拿 `fetch` 验"能不能跳"验的是另一条路径 |
| 停在 `about:blank` | 窗口确实在导航（303 → 跨域），只是脚本读不到结果 |

**共同点：判据依赖了"浏览器肯把跨域过程中的状态告诉我"。** 那条路本来就不保证。

### 二、服务端闭环（正式判据）

脚本：`.claude/skills/verify-aiops-gateway/scripts/observe_knowledge_open_closed_loop.py`

真机结果（在 41 上跑，用运营自己的 `system` 会话）：

| 判据 | 结果 |
|---|---|
| 桥：无会话 | 401 |
| 桥：有会话 → 303 且 `Location` 指向 `…/knowledge/hop?ticket=…` | 303 |
| 兑换：真票 → 303 且**种下会话** | 303，`set-cookie: session=…; Domain=.mall.qushiyun.com` |
| `Set-Cookie` 与跳转目标同域（ADR-0093） | `Domain=.mall.qushiyun.com` + `location: https://kb.mall.qushiyun.com` |
| 兑换：同一张票第二次 | **410** |
| 兑换：假票 | 410（不是 5xx） |

**这张表与"用户点一下"等价，但不依赖浏览器。**

### 三、写脚本时踩到的两个坑（记下来，因为它们会让判据假绿/假红）

1. **`urllib` 默认跟随 30x。** 第一版读到 200 判失败，而桥的日志明明写着 303 —— 它跟到了
   最终页。`303` **本身**就是证据，必须 `redirect_request → None`。
2. **`dict(response.headers)` 会丢重复键。** `Set-Cookie` 恰恰可能多值，而它正是判据本身。
   改成按小写键分组、多值用 `, ` 拼。

### 四、顺带更正一条过时的 nginx 注释

`dify-console-publish.conf` 里原写「租户由桥从 Cookie 解析，**不读任何 URL 参数** —— 所以这里
连 `$arg_*` 都不往下转」。

**两句都不对**：`location =` 精确匹配的是 **path**，query 原样保留；而桥**确实**读
`?workspace=`（页面带上来"我此刻在哪个 workspace"）。桥日志里能直接看到
`GET /__aiops/knowledge-open?workspace=c3f871e1-…`。

已改成准确的说法，并保留那条真正重要的边界：**它仍然不从 URL 取身份** —— 租户来自
Cookie 里的会话经登录代理验证；`workspace` 只是"这页看着是哪个"。

### 五、没验什么

- 非 Chromium 浏览器。
- `aiops-41-platform` workspace（未开户）。
- 脚本需要一份**只读的会话 cookie 文件**才能跑全 6 条；没有它时只跑无凭据那一条。
