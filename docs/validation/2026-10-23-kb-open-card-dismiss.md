## 2026-10-23 · 「卡片又没了」的第二条独立成因：点过 × 就永久消失（#1015）

**用户报告**：修好轮询之后，"怎么卡片又没了？"

### 一、先确认新版在线

```
curl https://dify.ranlei.work/__aiops/dify-knowledge-open.js | sha256sum
→ a712fc4f…a61a826d      # 与 #1013 落地的完全一致
grep tries / clearInterval  → 无
```

注入标签在（`<script src="/__aiops/dify-knowledge-open.js">`），登录态下实测
`card: true`。**所以不是 #1013 的回归。**

### 二、另一条路径：点过 ×

```js
function hide() { try { localStorage.setItem(HIDDEN_KEY, '1'); } catch (e) {} }
function hidden() { return localStorage.getItem(HIDDEN_KEY) === '1'; }
```

`×` 就在卡片右上角，一点就是**永久**，而页面上**没有任何恢复入口**。

真机复现（同一浏览器、同一账号）：

| 操作 | `card` | `localStorage['aiops.kbOpenCardHidden']` |
|---|---|---|
| 刚进页面 | true | null |
| 点 × 之后 | false | `'1'` |
| 刷新之后 | **false** | `'1'` |
| 全页面找恢复入口 | **无** | — |

用户看到的现象与 #1013 完全一样（"卡片又没了"），**根因毫无关系**。

### 三、为什么两轮修都没兜住

#1013 的教训写的是"验收要按用户的实际路径走"。这条更进一层：**验收要问"还有哪些
路径能让它消失"**，而不是只修自己复现的那一条。一个"从界面上足够显眼、从界面上无法
撤销"的状态，本身就是缺陷 —— 无论它是不是这次报告的那一个。

### 四、修法（方案 A）

收起只对**当前这次页面**有效：标记从 `localStorage` 改成一个内存变量
`dismissedThisPage`，刷新或重新进入即恢复。收起是"这次先别挡着我"，不是"我以后都不要了"。

两条旧断言因此需要同步改 —— 它们钉住的正是"收起必须落盘"这个后来被证明有害的形状：

- `test_the_card_can_be_dismissed_without_leaving_a_credential_behind`：原断言
  `setItem(HIDDEN_KEY, '1')` 在；改为断言**不落盘**且无身份。
- `test_the_script_degrades_silently_when_it_cannot_mount`：原来要求 `try {` 出现 2 次
  （localStorage 在隐私模式会抛）；去掉 localStorage 后只需 ≥1。

新增 `test_dismissing_the_card_is_scoped_to_one_page_visit`。

### 五、没验什么

修复要重新部署到 36 的 `/srv/aiops-console-assets/` 才生效；部署后要用「点 × → 刷新 →
卡片回来」这条路径复验。

### 六、部署与复验

桥的资源不在 CD 范围内（CD 只部署 41 的网关），要手工更新 36 的
`/srv/aiops-console-assets/`：

```bash
ssh yidong-36 'sudo cp -a /srv/aiops-console-assets/dify-knowledge-open.js \
  /srv/aiops-console-assets/dify-knowledge-open.js.bak-$(date +%Y%m%d-%H%M%S)'
scp ops/dify-console-bridge/dify-knowledge-open.js yidong-36:/tmp/dify-knowledge-open.js.new
ssh yidong-36 'sudo install -m 0644 -o root -g root /tmp/dify-knowledge-open.js.new \
  /srv/aiops-console-assets/dify-knowledge-open.js'
curl -s https://dify.ranlei.work/__aiops/dify-knowledge-open.js | sha256sum
```

判据（三条都要）：

1. 三方 sha256 一致（本地 / 落地 / 线上）；
2. 线上文件里 `localStorage` 与 `setItem` 均已消失；
3. **按本条缺陷的路径**复验：点 × → 刷新 → 卡片回来（旧版此处必定 `card:false`）。

**没验什么**：其它 workspace（`aiops-41-platform` 未开户）、非 Chromium 浏览器。
