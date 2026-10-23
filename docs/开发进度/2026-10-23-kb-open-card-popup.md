## 2026-10-23 · 「卡片在、点了没反应」（#1022）

**背景**：卡片回来了（#1021），但点「打开 RAGFlow」没有任何反应。

**取证**：card=true、href 正常、rect 正常可点；点击后 20 秒内无新标签，当前页 URL 也不变。

**根因**：开窗在异步回调里 ——

```js
ev.preventDefault();
currentWorkspace().then(function (ws) { window.open(url, '_blank', 'noopener'); });
```

浏览器的弹窗拦截只看「是否仍在用户手势有效期内」；`preventDefault()` 后马上进 `fetch`
就把信用用完了，等 promise 兑现再开窗被**静默**拦掉。而 `noopener` 让 `window.open`
本来返回 null，`if (!w)` 的兜底也一起静默。

**它时好时坏**（取决于 fetch 快慢），所以只跑一次的验收会偶然通过。

**修法**：先同步开 `about:blank` 保住手势，拿到 workspace 后把那个窗口 `location.href`
改成目标；真的被拦才本页跳。不传 `noopener` 才能拿到窗口引用 —— 代价写在代码注释里。

**这是这一轮的第三个独立成因**（轮询自杀 / 点 × 永久消失 / 开窗被拦），三者症状都是
"按钮没了或没反应"，根因互不相关。共同做法是**先取证再假设**。

**交付**：本分支；部署到 36 后按「点卡片 → 新标签 → 落在 RAGFlow 且已登录」复验。
