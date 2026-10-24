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
  var AIOPS_TRIAL_BTN_ID = 'aiops-trial-btn';
  var AIOPS_RETRACT_BTN_ID = 'aiops-retract-btn';
  var AIOPS_RESTORE_BTN_ID = 'aiops-restore-btn';

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

  /*
   * 「在 AI-Ops 试运行」（#781 / #782 的部署层）。
   *
   * **拿的是"最近一次保存"的配置，不是编辑框里此刻的内容。** Dify 1.17.1 的 chat 应用
   * 没有服务端草稿层：编辑页里没点过「发布更新」的改动只活在浏览器内存里，任何 API 都
   * 读不到（#773 实测）。所以按钮文案必须说清这件事，否则运营会以为它跑的是眼前这份。
   *
   * 订单号**只能由人来给**：编排页里没有订单上下文，而试运行必须落到一张具体订单上。
   */
  function aiopsTrial(btn) {
    if (!currentAppId()) { aiopsToast('请从某个智能体的编排页点这个按钮', false); return; }
    var orderNo = window.prompt(
      '在 AI-Ops 上用真实数据试运行这份草稿。\n\n' +
        '注意：以**最近一次保存**的配置为准（未点「发布更新」的改动 Dify 不保存）。\n' +
        '输入要试运行的订单号：'
    );
    if (orderNo === null) { return; }
    orderNo = (orderNo || '').trim();
    if (!orderNo) { aiopsToast('请填写订单号', false); return; }
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = '提交中…';
    fetch('/__aiops/trial', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        // 与发布同一枚 CSRF（桥用同一套浏览器三门）。
        'X-CSRF-Token': aiopsReadCsrfCookie(),
      },
      body: JSON.stringify({ order_no: orderNo }),
    })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        aiopsToast(d.reason || (d.submitted ? '已提交试运行' : '试运行没能开始'), !!d.submitted);
        // 只报"已提交"等于让运营自己去找结果 —— 而结果在网关的库里。有 trial_id
        // 就开一块面板轮询到终态；没有就不开（提交失败时没有可轮询的东西）。
        if (d.submitted && d.trial_id) { aiopsWatchTrial(d.trial_id, orderNo); }
      })
      .catch(function () { aiopsToast('试运行请求没能送达，请稍后再试', false); })
      .finally(function () { btn.disabled = false; btn.textContent = original; });
  }

  /*
   * 结果面板：把一次试运行从「已提交」跟到终态。
   *
   * 三个约束，都不是可选的：
   *   1. **终态由服务端说了算**（`terminal` 字段），前端不自己拼状态词表 —— 那会是
   *      第二份词表，而两份的表现是"面板永远转圈"或"跑到一半就停"。
   *   2. **有上限**：超过 `AIOPS_TRIAL_MAX_POLLS` 次就停，并说明"还在跑"。一个不设
   *      上限的轮询会在页面开着的时候永远发请求。
   *   3. **关掉面板就停**：`aiopsTrialTimer` 只有一处取消点，避免关掉之后仍在轮询。
   */
  var AIOPS_TRIAL_PANEL_ID = 'aiops-trial-panel';
  var AIOPS_TRIAL_POLL_MS = 2000;
  var AIOPS_TRIAL_MAX_POLLS = 150; // 150 × 2s = 5 分钟，够一次真实诊断跑完
  var aiopsTrialTimer = null;

  function aiopsTrialPanel() {
    var panel = document.getElementById(AIOPS_TRIAL_PANEL_ID);
    if (panel) { return panel; }
    panel = document.createElement('div');
    panel.id = AIOPS_TRIAL_PANEL_ID;
    panel.style.cssText =
      'position:fixed;z-index:99998;right:24px;bottom:80px;width:420px;max-width:calc(100vw - 48px);' +
      'background:#fff;color:#101828;border:1px solid #d0d5dd;border-radius:10px;' +
      'box-shadow:0 8px 28px rgba(16,24,40,.18);font-size:13px;line-height:1.6;overflow:hidden;';
    panel.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;' +
      'padding:10px 14px;background:#f9fafb;border-bottom:1px solid #eaecf0;">' +
      '<strong style="font-size:13px;">AI-Ops 试运行</strong>' +
      '<button type="button" data-aiops-close="1" style="border:0;background:transparent;' +
      'cursor:pointer;font-size:16px;line-height:1;color:#667085;">×</button></div>' +
      '<div data-aiops-body="1" style="padding:12px 14px;max-height:280px;overflow:auto;' +
      'white-space:pre-wrap;word-break:break-word;"></div>';
    panel.querySelector('[data-aiops-close]').addEventListener('click', function () {
      aiopsTrialStop();
      panel.remove();
    });
    document.body.appendChild(panel);
    return panel;
  }

  function aiopsTrialStop() {
    if (aiopsTrialTimer !== null) { clearTimeout(aiopsTrialTimer); aiopsTrialTimer = null; }
  }

  function aiopsTrialRender(lines) {
    var panel = aiopsTrialPanel();
    panel.querySelector('[data-aiops-body]').textContent = lines.join('\n');
  }

  function aiopsWatchTrial(trialId, orderNo) {
    aiopsTrialStop();
    var polls = 0;
    aiopsTrialRender(['订单 ' + orderNo, '已提交，等待结果…', '', '作业 ' + trialId]);

    function once() {
      fetch('/__aiops/trial/result', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': aiopsReadCsrfCookie(),
        },
        body: JSON.stringify({ trial_id: trialId }),
      })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          polls += 1;
          var head = ['订单 ' + orderNo, '', d.reason || '（无结果）'];
          if (!d.read) {
            // 读失败不是终态：可能是作业还没落库/网关瞬时不可用。继续轮，但有上限。
            head.push('', '（第 ' + polls + ' 次读取未成功）');
          }
          aiopsTrialRender(head.concat(['', '作业 ' + trialId]));
          if (d.read && d.terminal) { aiopsTrialStop(); return; }
          if (polls >= AIOPS_TRIAL_MAX_POLLS) {
            aiopsTrialRender(
              head.concat(['', '已停止自动刷新（超过 5 分钟）。作业 ' + trialId])
            );
            aiopsTrialStop();
            return;
          }
          aiopsTrialTimer = setTimeout(once, AIOPS_TRIAL_POLL_MS);
        })
        .catch(function () {
          // 网络抖动：继续按节拍重试，直到上限。面板上不堆错误，避免刷屏。
          polls += 1;
          if (polls >= AIOPS_TRIAL_MAX_POLLS) { aiopsTrialStop(); return; }
          aiopsTrialTimer = setTimeout(once, AIOPS_TRIAL_POLL_MS);
        });
    }

    once();
  }

  function aiopsMakeButton(id, label, bg) {
    var btn = document.createElement('button');
    btn.id = id;
    btn.type = 'button';
    btn.textContent = label;
    btn.style.cssText =
      'margin-right:8px;padding:0 14px;height:32px;border-radius:6px;cursor:pointer;' +
      'border:1px solid ' + bg + ';background:' + bg + ';color:#fff;font-size:13px;';
    return btn;
  }

  function aiopsManage(btn, operation) {
    var body = {};
    var choose = Promise.resolve();
    if (operation === 'retract') {
      if (!window.confirm('撤回后将停止交付当前版本，是否继续？')) { return; }
    }
    if (operation === 'restore') {
      choose = fetch('/__aiops/versions', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': aiopsReadCsrfCookie() },
        body: '{}',
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.published || !d.versions || !d.versions.length) { throw new Error('没有可恢复的版本'); }
        var choices = d.versions.map(function (v) { return v.version_no; });
        var selected = window.prompt('选择历史版本：' + choices.join('、'));
        if (selected === null) { throw new Error('已取消'); }
        body.version_no = Number(selected);
        if (choices.indexOf(body.version_no) < 0) { throw new Error('请选择列表中的版本'); }
      });
    }
    btn.disabled = true;
    choose.then(function () {
      return fetch('/__aiops/' + operation, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': aiopsReadCsrfCookie() },
        body: JSON.stringify(body),
      });
    }).then(function (r) { return r.json(); }).then(function (d) {
      aiopsToast(d.reason || '操作未完成', !!d.published);
    }).catch(function (e) { aiopsToast(e.message || '操作未完成', false); })
      .finally(function () { btn.disabled = false; });
  }

  function aiopsMount() {
    if (!currentAppId()) { return; }          // 只在编排页
    // 两颗按钮各自去重：只查发布那颗会让试运行那颗在重试中被挂第二遍。
    if (
      document.getElementById(AIOPS_BTN_ID) &&
      document.getElementById(AIOPS_TRIAL_BTN_ID) &&
      document.getElementById(AIOPS_RETRACT_BTN_ID) && document.getElementById(AIOPS_RESTORE_BTN_ID)
    ) {
      return;
    }
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
    // 试运行在前（它是更轻的那个动作），发布在后，都插在 Dify 原生按钮之前。
    if (!document.getElementById(AIOPS_TRIAL_BTN_ID)) {
      var trialBtn = aiopsMakeButton(AIOPS_TRIAL_BTN_ID, '在 AI-Ops 试运行', '#0b6ba8');
      trialBtn.addEventListener('click', function () { aiopsTrial(trialBtn); });
      anchor.parentNode.insertBefore(trialBtn, anchor);
    }
    [['retract', AIOPS_RETRACT_BTN_ID, '撤回正式版本'], ['restore', AIOPS_RESTORE_BTN_ID, '恢复历史版本']]
      .forEach(function (item) {
        if (document.getElementById(item[1])) { return; }
        var actionBtn = aiopsMakeButton(item[1], item[2], '#475467');
        actionBtn.addEventListener('click', function () { aiopsManage(actionBtn, item[0]); });
        anchor.parentNode.insertBefore(actionBtn, anchor);
      });
    if (document.getElementById(AIOPS_BTN_ID)) { return; }
    var btn = aiopsMakeButton(AIOPS_BTN_ID, '发布到 AI-Ops', '#12805c');
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
    // 两颗都挂上才停；只看发布那颗会让试运行那颗在第一颗挂上后就再也不重试。
    var mounted =
      document.getElementById(AIOPS_BTN_ID) && document.getElementById(AIOPS_TRIAL_BTN_ID) &&
      document.getElementById(AIOPS_RETRACT_BTN_ID) && document.getElementById(AIOPS_RESTORE_BTN_ID);
    if (mounted || tries > 40) { clearInterval(timer); }
  }, 500);
})();
