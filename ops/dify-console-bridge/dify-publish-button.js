/* AI-Ops 注入到 Dify 编排页的「发布到 AI-Ops」按钮（ADR-0021）。
 *
 * 三件事，都很小：
 *   1. 只在 /app/<id>/configuration 这一页出现（发布的是"当前这个智能体"）；
 *   2. 点它 → 打同源的 /__aiops/publish（浏览器自动带 Dify 会话 cookie）；
 *   3. 把结果用一句话告诉运营。
 *
 * **它不含任何凭据。** 真正那把发布密钥在服务端的桥里。
 * **它不做任何自动行为。** 不点，什么都不会发生 —— 包括 Dify 自己的自动保存。
 */
(function () {
  'use strict';
  var AIOPS_APP_RE = /^\/app\/([0-9a-fA-F-]{8,64})\//;
  var AIOPS_BTN_ID = 'aiops-publish-btn';

  function currentAppId() {
    var m = AIOPS_APP_RE.exec(location.pathname);
    return m ? m[1] : null;
  }

  function aiopsReadCookie(name) {
    var parts = document.cookie.split(';');
    for (var i = 0; i < parts.length; i++) {
      var kv = parts[i].trim().split('=');
      if (kv[0] === name) { return decodeURIComponent(kv.slice(1).join('=')); }
    }
    return '';
  }

  /*
   * Dify 的 cookie 名**随 scheme 变**（`api/libs/token.py`）：https 且未设
   * COOKIE_DOMAIN 时加 `__Host-` 前缀，http 下是裸名。Dify 自己的前端也是这么
   * 分支的（`web/config/index.ts` 的 `isSecure ? '__Host-csrf_token' : ...`）。
   *
   * ⚠️ **两种都要试，且不能只试一种。** #776 把控制台收到 https 后，裸名会变成
   * `__Host-csrf_token`；只读裸名会拿到空串 → 提交空 CSRF → 桥 401，而**界面上
   * 看不出为什么**（按钮还在、点了就失败）。这与桥侧的解析口径一致
   * （`dify_console_publish.py` 的 `console_session_from_headers` 同时识别两种前缀）。
   */
  function aiopsReadCsrfCookie() {
    return aiopsReadCookie('__Host-csrf_token') || aiopsReadCookie('csrf_token');
  }

  function aiopsToast(msg, ok) {
    var el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText =
      'position:fixed;z-index:99999;right:24px;bottom:24px;max-width:420px;' +
      'padding:12px 16px;border-radius:8px;font-size:13px;line-height:1.5;' +
      'box-shadow:0 4px 16px rgba(0,0,0,.18);color:#fff;' +
      'background:' + (ok ? '#12805c' : '#b42318') + ';';
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, ok ? 4000 : 7000);
  }

  function aiopsPublish(btn) {
    var appId = currentAppId();
    if (!appId) { aiopsToast('请从某个智能体的编排页点这个按钮', false); return; }
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = '发布中…';
    fetch('/__aiops/publish', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        // Dify 的 console API 要求这个头（csrf_token cookie 的副本）—— 缺了它会
        // 401 CSRF token is missing。桥把它**原样**转给 Dify 去验。
        'X-CSRF-Token': aiopsReadCsrfCookie(),
      },
      body: JSON.stringify({}),
    })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) { aiopsToast(d.reason || (d.published ? '已发布' : '发布失败'), !!d.published); })
      .catch(function () { aiopsToast('发布请求没能送达，请稍后再试', false); })
      .finally(function () { btn.disabled = false; btn.textContent = original; });
  }

  function aiopsMount() {
    if (!currentAppId()) { return; }          // 只在编排页
    if (document.getElementById(AIOPS_BTN_ID)) { return; }
    // 挂靠点：包含「发布/Publish」那个按钮的**同一个 flex 容器**，
    // 插在它前面。选择器是实测出来的（Dify 1.17.1 的编排页顶栏）；
    // 找不到就静默不挂 —— 没有按钮，Dify 照常。
    var publish = null;
    var all = document.querySelectorAll('button');
    for (var i = 0; i < all.length; i++) {
      var t = (all[i].textContent || '').trim();
      if (t === '发布' || t === 'Publish' || t === '发布更新' || t === 'Publish Update') {
        publish = all[i];
        break;
      }
    }
    if (!publish || !publish.parentNode) { return; }
    var anchor = publish;
    var btn = document.createElement('button');
    btn.id = AIOPS_BTN_ID;
    btn.type = 'button';
    btn.textContent = '发布到 AI-Ops';
    btn.style.cssText =
      'margin-right:8px;padding:0 14px;height:32px;border-radius:6px;cursor:pointer;' +
      'border:1px solid #12805c;background:#12805c;color:#fff;font-size:13px;';
    btn.addEventListener('click', function () { aiopsPublish(btn); });
    anchor.parentNode.insertBefore(btn, anchor);
  }

  /*
   * Dify 是 SPA，路由变化不触发 load。用**只在页面里**跑的观察器挂载。
   * 挂不上就是挂不上 —— 静默降级：没有按钮，Dify 照常。
   */
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    try { aiopsMount(); } catch (e) { /* 静默降级 */ }
    if (document.getElementById(AIOPS_BTN_ID) || tries > 40) { clearInterval(timer); }
  }, 500);
})();
