## 2026-10-23 · 「卡片在、点了没反应」：开窗落在异步回调里被静默拦截（#1022）

**用户报告**：卡片回来了，但点「打开 RAGFlow」没有反应。

### 一、取证

```
card:   true
href:   "/__aiops/knowledge-open"
text:   "打开 RAGFlow"
rect:   {x:1335, y:1036, w:112, h:30}       ← 位置正常，点得到
→ 点击后 20 秒内没有新标签，当前页 URL 也不变
```

一切"看起来正常"：元素在、可点、链接对。

### 二、根因

```js
ev.preventDefault();
currentWorkspace().then(function (ws) {
  var w = window.open(url, '_blank', 'noopener');   // ← 在异步回调里
  if (!w) { location.href = url; }
});
```

浏览器的弹窗拦截只看「这次 `window.open` 是否仍在**用户手势**的有效期内」。
`preventDefault()` 之后紧接着进 `fetch`，手势的信用就被用完了；等 promise 兑现再开窗，
被**静默**拦掉 —— 不报错、不开窗、什么都不发生。

而 `window.open(..., 'noopener')` 本来就**返回 null**，所以 `if (!w)` 那句兜底在
正常路径上也会被触发……但它同样在 promise 里，于是两件事一起静默。

**它时好时坏**：取决于 fetch 快慢。所以只跑一次的验收会偶然通过 —— 之前的验收过了，
不代表它对。

### 三、修法

把开窗挪回**点击的同一个同步回合**：

```js
var w = window.open('about:blank', '_blank');     // 先保住手势
... currentWorkspace().then(navigate, function(){ navigate(''); });
function navigate(ws) {
  var url = target + (ws ? '?workspace=' + encodeURIComponent(ws) : '');
  if (w) { try { w.location.href = url; return; } catch (e) {} }
  location.href = url;                            // 真的被拦才本页跳
}
```

**代价写在代码里**：不传 `noopener` 才能拿到窗口引用（传了必返回 null），于是新标签
能通过 `window.opener` 指回本页。那个引用于我们毫无用处；而"能区分开窗成功与被拦"
是必须的 —— 否则下次再坏还是一片静默。

### 四、这一轮的第三个独立成因

| # | 成因 | 修在 |
|---|---|---|
| 1 | 轮询 20 秒后自杀 | #1013 |
| 2 | 点 × 永久消失 | #1021 |
| 3 | `window.open` 在异步回调里被拦 | 本条 |

三者症状都是"按钮没了/没反应"，根因互不相关。**共同方法是：先取证，再假设。**
每一次都是先看到"元素在不在、点得到不到、点什么发了什么"，而不是先猜一个原因改一版。

### 五、没验什么

修复要部署到 36 的 `/srv/aiops-console-assets/` 才生效；部署后按「点卡片 → 新标签 →
落在 RAGFlow 且已登录」复验。其它浏览器（非 Chromium）未验。
