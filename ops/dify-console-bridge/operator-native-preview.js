/* 管家原生预览复用节点试用；请求只带草稿和问题，公司凭据始终留在服务端。 */
(function () {
  'use strict';
  var originalFetch = window.fetch.bind(window);
  window.fetch = async function (input, options) {
    var request = input instanceof Request ? input : null;
    var url = new URL(request ? request.url : String(input), location.origin);
    var method = (options && options.method) || (request && request.method) || 'GET';
    var match = /^\/console\/api\/apps\/([0-9a-f-]{36})\/chat-messages$/.exec(url.pathname);
    var stop = /^\/console\/api\/apps\/([0-9a-f-]{36})\/chat-messages\/(aiops_qa_[a-zA-Z0-9]+)\/stop$/.exec(url.pathname);
    if (url.origin !== location.origin || method.toUpperCase() !== 'POST' || (!match && !stop)) {
      return originalFetch(input, options);
    }
    var body;
    try { body = JSON.parse((options && options.body) || (request && await request.clone().text()) || '{}'); } catch (_) { return originalFetch(input, options); }
    var config = body.model_config || {};
    var tools = (config.agent_mode || {}).tools || [];
    if (!Array.isArray(tools)) { return originalFetch(input, options); }
    // 仅识别候选请求，provider UUID 和应用归属由 Gateway 校验。
    if (!stop && !tools.some(function (t) { return t.provider_type === 'api' && (t.tool_name === 'cpms_order_detail' || t.tool_name === 'cpms_charging_samples'); })) {
      return originalFetch(input, options);
    }
    if (!stop) {
      var providersResponse = await originalFetch('/console/api/workspaces/current/tool-providers?type=api', {
        credentials: 'same-origin', headers: (options && options.headers) || (request && request.headers),
        signal: (options && options.signal) || (request && request.signal),
      });
      if (!providersResponse.ok) { return providersResponse; }
      var providers = await providersResponse.json();
      var provider = Array.isArray(providers) && providers.find(function (p) { return p.name === 'cpms-node'; });
      if (!provider || !tools.some(function (t) { return t.provider_id === provider.id; })) {
        return originalFetch(input, options);
      }
    }
    if (!stop && body.files && body.files.length) {
      return Promise.resolve(new Response(JSON.stringify({code: 'OPERATOR_PREVIEW_FILES_UNSUPPORTED', message: '管家预览仅支持明确订单，暂不接受附件'}), {status: 400, headers: {'Content-Type': 'application/json'}}));
    }
    if (typeof document !== 'undefined' && document.querySelector && document.querySelector('[data-aiops-managed="true"]')) return originalFetch(input, options);
    var payload = stop ? {task_id: stop[2]} : {
      app_id: match[1], query: body.query, pre_prompt: config.pre_prompt,
      tools: tools.map(function (t) {
        return {provider_id: t.provider_id, provider_type: t.provider_type, tool_name: t.tool_name, enabled: t.enabled !== false};
      }),
    };
    return originalFetch(stop ? '/__aiops/operator-preview/cancel' : '/__aiops/operator-preview',
      Object.assign({}, request ? {method: request.method, headers: request.headers, credentials: request.credentials, signal: request.signal} : {}, options, {body: JSON.stringify(payload)}));
  };
})();
