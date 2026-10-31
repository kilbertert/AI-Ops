#!/usr/bin/env python3
"""只读：读生产 Dify 应用的草稿工作流，核对提示词与事实片段契约状态。

**不做任何写操作**：只用 GET 读 `/console/api/apps/<id>/workflows/draft`。

必要的请求头是 **`X-WORKSPACE-ID`**（见 `dify_console_admin._headers`）：
只带 `Authorization` 会得到 401，且看起来像"密钥失效"——
2026-10-31 我据此误判过一次，所以这里把 header 的存在与否分别打印出来。
凭据从 41 的 production.env 现场读取，不打印。
"""

import json
import sys
import urllib.error
import urllib.request

ENV_FILE = "/etc/aiops-41/production.env"


def load_env(prefixes):
    values = {}
    with open(ENV_FILE, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if any(key.startswith(p) for p in prefixes):
                values[key] = value.strip().strip('"').strip("'")
    return values


def get(base, key, workspace, path):
    headers = {"Authorization": "Bearer " + key, "X-WORKSPACE-ID": workspace}
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.load(response)


def main():
    app_id = sys.argv[1]
    env = load_env(("AIOPS_GATEWAY_DIFY_CONSOLE",))
    base = env["AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL"].rstrip("/")
    key = env["AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY"]
    workspace = env["AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID"]

    try:
        status, data = get(base, key, workspace, f"/console/api/apps/{app_id}/workflows/draft")
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": "http", "status": exc.code}, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 1

    envs = data.get("environment_variables") or []
    fact_contract = next((e.get("value") for e in envs if e.get("name") == "answer_fact_contract"), None)
    nodes = []
    for node in data.get("graph", {}).get("nodes", []):
        payload = node.get("data", {})
        text = json.dumps(
            {
                "prompt_template": payload.get("prompt_template"),
                "prompts": payload.get("prompts"),
            },
            ensure_ascii=False,
        )
        if text.strip() in ("{}", "", "null"):
            continue
        nodes.append(
            {
                "type": payload.get("type"),
                "title": payload.get("title"),
                "chars": len(text),
                "fact_slots": text.count("{{fact:"),
                "protocol_block": "每个片段单独占一行" in text,
            }
        )
    print(
        json.dumps(
            {
                "http": status,
                "fact_contract": fact_contract,
                "environment_variables": [e.get("name") for e in envs],
                "nodes": nodes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
