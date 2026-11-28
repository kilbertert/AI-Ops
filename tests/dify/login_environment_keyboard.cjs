// Exercise the inline login script with synthetic environments and no network.
const assert = require("node:assert/strict");
const fs = require("node:fs");

const run = async (page, assert) => {
  class Element {
    constructor() {
      this.attrs = new Map();
      this.children = [];
      this.events = new Map();
      this.style = {};
      this.value = "";
    }
    setAttribute(key, value) { this.attrs.set(key, value); }
    getAttribute(key) { return this.attrs.get(key); }
    removeAttribute(key) { this.attrs.delete(key); }
    addEventListener(type, handler) { this.events.set(type, handler); }
    dispatchEvent(event) { this.events.get(event.type)?.(event); }
    appendChild(child) {
      this.children.push(child);
      if (this.id === "environment" && !this.value) this.value = child.value;
    }
    querySelectorAll() { return this.children; }
    scrollIntoView() {}
    focus() {}
  }
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, Object.assign(new Element(), { id }));
    return elements.get(id);
  };
  const document = Object.assign(new Element(), {
    getElementById: get,
    createElement: () => new Element(),
    createElementNS: () => new Element()
  });
  const requests = [];
  class Image extends Element {
    set src(path) { requests.push(decodeURIComponent(path.split("&environment=")[1])); }
  }
  get("env-listbox").hidden = true;
  const window = { location: { search: "" }, crypto: { getRandomValues() {} } };
  const fetch = async () => ({
    ok: true,
    json: async () => ({
      environments: ["A", "B", "C"].map((id) => ({ id, name: id })),
      selection_required: true
    })
  });
  const script = page.split("<script>")[1].split("</script>")[0];
  new Function("document", "window", "navigator", "fetch", "Image", "Event", "URLSearchParams", script)(
    document, window, {}, fetch, Image, class { constructor(type) { this.type = type; } },
    class { get() { return null; } }
  );
  for (let i = 0; i < 8; i++) await Promise.resolve();

  const trigger = get("env-trigger");
  const list = get("env-listbox");
  const env = get("environment");
  const code = get("code");
  const press = (key) => trigger.dispatchEvent({ type: "keydown", key, preventDefault() {} });
  assert.deepEqual(requests, ["A"]);
  code.value = "pending";
  press("ArrowDown");
  press("ArrowDown");
  assert.equal(env.value, "A");
  assert.equal(list.hidden, false);
  assert.equal(trigger.getAttribute("aria-activedescendant"), list.children[1].id);
  assert.equal(list.children[0].getAttribute("aria-selected"), "true");
  assert.equal(code.value, "pending");
  press("Escape");
  assert.equal(list.hidden, true);
  assert.equal(trigger.getAttribute("aria-activedescendant"), undefined);
  assert.equal(env.value, "A");
  assert.deepEqual(requests, ["A"]);

  press("ArrowDown");
  press("End");
  press("ArrowUp");
  press("Enter");
  assert.equal(env.value, "B");
  assert.equal(list.hidden, true);
  assert.equal(code.value, "");
  assert.deepEqual(requests, ["A", "B"]);

  press("ArrowDown");
  press(" ");
  assert.deepEqual(requests, ["A", "B"], "confirming the current value must not refresh");
  press("ArrowDown");
  press("ArrowDown");
  press("Tab");
  assert.equal(list.hidden, true);
  assert.deepEqual(requests, ["A", "B"]);
  trigger.dispatchEvent({ type: "click" });
  list.children[2].dispatchEvent({ type: "click" });
  assert.equal(env.value, "C");
  assert.deepEqual(requests, ["A", "B", "C"]);
};

run(fs.readFileSync(process.argv[2], "utf8"), assert).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
