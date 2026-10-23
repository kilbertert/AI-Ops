/* AI-Ops 注入到 Dify 知识库页的「在 RAGFlow 中管理知识库」入口（PRD #981 / #983）。
 *
 * 三件事，都很小：
 *   1. 只在**知识库页**出现；
 *   2. 点它 → 新标签打开 `/__aiops/knowledge-open`（桥验完会话后 303 到该租户的 RAGFlow）；
 *   3. 运营不想看时可以收起。
 *
 * **它不含任何凭据。** 租户由桥从浏览器本来就带的 Dify 会话里解析，脚本一个字节的
 * 身份信息都不传 —— 传了就等于让 URL 决定"进哪个租户"，而那正是 #979 那个洞的形状。
 *
 * **它不碰 Dify 的 DOM 结构。** 知识库页顶栏没有可依附的同级按钮（发布按钮那种挂法是
 * 靠"找到「发布」按钮、插在它前面"，这里没有等价物），所以这里是一张**固定在右下角的
 * 小卡片**：不插入 Dify 的布局树、不覆盖它的点击区、Dify 改版也只会让它"位置不理想"，
 * 不会让它消失或把页面搞坏。
 *
 * **挂不上就是挂不上。** 找不到 body 就静默退出，样式全部隔离在一个 id 前缀下。
 */
(function () {
  'use strict';
  var CARD_ID = 'aiops-kb-open-card';
  var HIDDEN_KEY = 'aiops.kbOpenCardHidden';

  /* 只在知识库页出现。
   *
   * **前缀精确匹配**，不是 `indexOf('/datasets')` —— 后者会命中 `/datasets-x` 这种
   * 不存在的路径（还好），也会让一个以后新增的 `/apps/datasets` 误中（不好）。
   * Dify 1.17.1 的知识库路由是 `/datasets` 与 `/datasets/<id>/documents`。
   */
  function onKnowledgePage(pathname) {
    return pathname === '/datasets' || pathname.indexOf('/datasets/') === 0;
  }

  function hidden() {
    try { return localStorage.getItem(HIDDEN_KEY) === '1'; } catch (e) { return false; }
  }

  function hide() {
    try { localStorage.setItem(HIDDEN_KEY, '1'); } catch (e) { /* 存不了就算了 */ }
  }

  function build() {
    var card = document.createElement('div');
    card.id = CARD_ID;
    // 全部内联样式：不依赖 Dify 的 CSS 变量与 Tailwind 类名（它们会随版本改）。
    card.style.cssText = [
      'position:fixed',
      'right:20px',
      'bottom:20px',
      'z-index:2147483000',
      'background:#1c1f26',
      'color:#f2f4f8',
      'border:1px solid #3a3f4b',
      'border-radius:10px',
      'padding:12px 14px',
      'font:13px/1.5 -apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif',
      'box-shadow:0 6px 24px rgba(0,0,0,.35)',
      'max-width:260px',
    ].join(';');

    var title = document.createElement('div');
    title.textContent = '知识库在 RAGFlow 里管理';
    title.style.cssText = 'font-weight:600;margin-bottom:4px';
    card.appendChild(title);

    var hint = document.createElement('div');
    hint.textContent = '建库、传文档、解析都在 RAGFlow 完成，回来这里就能选。';
    hint.style.cssText = 'color:#9aa4b2;font-size:12px;margin-bottom:10px';
    card.appendChild(hint);

    var open = document.createElement('a');
    open.textContent = '打开 RAGFlow';
    // 真正要去的地址在点击时才算（见 onClick）—— 因为要知道"我此刻在哪个 workspace"，
    // 而那需要先问 Dify。href 留一个可访问的兜底，右键复制也不会是死链。
    open.href = '/__aiops/knowledge-open';
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    open.addEventListener('click', function (ev) {
      // **页面自己问 Dify"我在哪个 workspace"，再把答案带上。**
      //
      // 只有页面能问：Dify 的 `/console/api/workspaces/current/summary` 要 CSRF
      // 双提交，而 CSRF 头只能由页面脚本从 cookie 里读出来发出去；桥那边收不到。
      // 桥也不该去猜 —— 它拿公司身份推过一次，退回来是 `tenant_id=-1`（平台账号），
      // 换不出任何租户。切在哪，就管哪个。
      ev.preventDefault();
      currentWorkspace().then(function (ws) {
        var url = '/__aiops/knowledge-open' + (ws ? '?workspace=' + encodeURIComponent(ws) : '');
        var w = window.open(url, '_blank', 'noopener');
        if (!w) { location.href = url; }  // 弹窗被拦：退化成当前页跳转
      });
    });
    open.style.cssText = [
      'display:inline-block',
      'background:#2f6feb',
      'color:#fff',
      'text-decoration:none',
      'padding:6px 12px',
      'border-radius:6px',
      'font-size:12.5px',
    ].join(';');
    card.appendChild(open);

    var close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', '收起');
    close.style.cssText = [
      'position:absolute',
      'top:6px',
      'right:8px',
      'background:transparent',
      'border:0',
      'color:#9aa4b2',
      'font-size:15px',
      'cursor:pointer',
      'line-height:1',
      'padding:2px',
    ].join(';');
    close.addEventListener('click', function () {
      hide();
      card.remove();
    });
    // relative 是给这个绝对定位的关闭键用的；写在最后以免覆盖上面的声明。
    card.style.position = 'fixed';
    card.appendChild(close);
    return card;
  }

  /* 问 Dify：当前 workspace 是哪个。
   *
   * 用它自己的接口 + 它自己的双提交规则（CSRF 头取自 cookie）。**拿不到就返回空串**，
   * 让桥回一句可读的话 —— 而不是带一个错的 workspace 过去。
   */
  function currentWorkspace() {
    var csrf = '';
    var m = document.cookie.match(/(?:^|;\s*)(?:__Host-)?csrf_token=([^;]+)/);
    if (m) { csrf = decodeURIComponent(m[1]); }
    return fetch('/console/api/workspaces/current/summary', {
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (data) {
      return (data && data.id) || '';
    }).catch(function () {
      return '';
    });
  }

  function mount() {
    if (!onKnowledgePage(location.pathname)) {
      var stale = document.getElementById(CARD_ID);
      if (stale) { stale.remove(); }
      return true; // 不在这一页 = 没什么可做，也算"完成"
    }
    if (hidden() || document.getElementById(CARD_ID) || !document.body) {
      return true;
    }
    document.body.appendChild(build());
    return true;
  }

  /*
   * Dify 是 SPA，路由变化不触发 load。用轮询而不是 MutationObserver：观察器会在
   * Dify 每次重绘时回调很多次，而这里要的只是"每半秒看一眼我在哪一页"。
   * 40 次（20 秒）之后停 —— 一直转下去的定时器会白白耗电。
   */
  if (mount()) {
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      try { mount(); } catch (e) { /* 静默降级 */ }
      if (tries > 40) { clearInterval(timer); }
    }, 500);
  }
})();
