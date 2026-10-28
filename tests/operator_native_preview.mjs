import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const calls = [];
const origin = 'https://console.example';
globalThis.location = { origin };
globalThis.window = {
  fetch: async (url, options) => {
    calls.push({ url, options });
    return new Response('data: {"event":"message_end"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  },
};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const app = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const url = `${origin}/console/api/apps/${app}/chat-messages`;
const model_config = { pre_prompt: 'UNSAVED', agent_mode: { tools: [{ provider_name: 'cpms-node', provider_id: 'uuid', provider_type: 'api', tool_name: 'cpms_order_detail', enabled: true }] } };
const headers = { 'X-CSRF-Token': 'own-csrf', 'Content-Type': 'application/json' };
const body = JSON.stringify({ query: 'order 2115330675982561281', model_config });
await window.fetch(url, { method: 'POST', headers, body });
assert.equal(calls[0].url, '/__aiops/operator-preview');
assert.equal(JSON.parse(calls[0].options.body).pre_prompt, 'UNSAVED');
assert.equal(JSON.parse(calls[0].options.body).tools[0].provider_id, 'uuid');
assert.equal(calls[0].options.headers['X-CSRF-Token'], 'own-csrf');
await window.fetch(new Request(url + '/aiops_qa_test1/stop', { method: 'POST', headers }));
assert.equal(calls[1].url, '/__aiops/operator-preview/cancel');
assert.equal(new Headers(calls[1].options.headers).get('X-CSRF-Token'), 'own-csrf');
await window.fetch(url, { method: 'POST', headers, body: JSON.stringify({ query: 'ordinary', model_config: {} }) });
assert.equal(calls[2].url, url);
await window.fetch(url.replace(origin, 'https://other.example'), { method: 'POST', headers, body });
assert.ok(calls[3].url.startsWith('https://other.example'));
const files = await window.fetch(url, { method: 'POST', headers, body: JSON.stringify({ model_config, files: [{}] }) });
assert.equal(files.status, 400);
assert.equal(calls.length, 4);
const abort = new AbortController();
await window.fetch(url, { method: 'POST', headers, body, signal: abort.signal });
assert.equal(calls[4].options.signal, abort.signal);
console.log('native preview, Request stop, ordinary preview, origin, files, abort: passed');
