"""平台初始化（在 Gateway 容器里执行）。**只用平台自己的管理接口**，不写它的 MySQL。

三件事，按顺序，全部幂等：

1. 换掉默认管理员口令。schema 里是 `admin/admin123`；轮换目标由 bootstrap.sh
   预先保存在宿主，再通过 `/tmp/platform_admin` 传入。
2. 禁用 schema 自带的 demo 应用（`snail-ai-agent-demo` 的固定 token）。
3. 建本栈的应用 / 模型配置 / 智能体 / OpenAPI 用户与订阅，并把 appId、agentId、
   app token、openId 回填进绑定表。

`--check` 模式下不建任何东西，只验证默认凭据已经不可用。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 平台讲 HTTPS（证书由 make-certs.sh 生成，SAN 含 platform）；网关容器里用
# SSL_CERT_FILE 指向本栈 CA。**不经过回环中继**，也不关闭校验。
BASE = os.environ.get("M2LIVE_PLATFORM_URL", "https://platform:8900").rstrip("/") + "/snail-ai"
SERVICE = os.environ.get("M2LIVE_SERVICE", "aiops-m2live03")
# 供应商实际收到的 `model` 字段。**不是**内部资源名：把它写成项目名会让供应商收到一个
# 它不认识的模型标识。正式模式必须显式给出；合成模式由 bootstrap.sh 传桩模型名。
MODEL_ID = os.environ.get("M2LIVE_MODEL_ID", "").strip()
MODE = os.environ.get("M2LIVE_BOOTSTRAP_MODE", "apply")
ADMIN_FILE = "/tmp/platform_admin"
BINDINGS_IN = "/tmp/agent-runs.in.json"
BINDINGS_OUT = "/tmp/agent-runs.out.json"
DEFAULT_PASSWORD = "admin123"
DEMO_APP = "snail-ai-agent-demo"
ENVIRONMENT = "live-trial-41"
EXPECTED_NODE = "node-41"


def call(path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, None


def fail(message: str) -> None:
    print(f"bootstrap: {message}", file=sys.stderr)
    raise SystemExit(1)


def login(password: str):
    status, body = call("/user/login", {"username": "admin", "password": password})
    if status == 200 and body and body.get("status") == 1:
        return body["data"]["token"]
    return None


def _fresh_database() -> bool:
    """平台库里是否还**只有** schema 自带的默认管理员。

    「口令文件与库不一致」只在库被重建（例如 `down --purge` 之后换了一套状态）时是
    正常情形；那种情况下默认口令仍是 `admin123`，可以安全地重新轮换。库里有别的用户
    时不做任何猜测 —— 那是需要人工处理的情形。
    """
    return login(DEFAULT_PASSWORD) is not None


def admin_token() -> str:
    """优先用已经换过的口令；没有就退回默认口令（只在首次/重建时成立）。"""
    if os.path.exists(ADMIN_FILE):
        token = login(Path(ADMIN_FILE).read_text().strip())
        if token:
            return token
        if not _fresh_database():
            fail(
                "已保存的管理员口令登录失败，且默认口令也不可用："
                "该口令文件不是这台平台的口令，需要人工恢复管理员"
            )
        # 库是新的：把陈旧的口令文件丢掉，走一次正常轮换。
        os.unlink(ADMIN_FILE)
        print("bootstrap: 口令文件与平台不匹配，但库仍是默认状态 —— 重新轮换", flush=True)
    token = login(DEFAULT_PASSWORD)
    if token is None:
        fail("默认管理员凭据不可用且没有已保存的新口令：需要人工恢复平台管理员")
    return token


def ensure_admin_rotated() -> str:
    """确保默认口令**已经**被换掉，并返回可用令牌。

    三种情形：口令文件可用 ⇒ 直接用；口令文件失效但库仍是默认状态（重建过）⇒
    使用宿主预先保存的口令轮换；口令文件失效且默认口令也不可用 ⇒ 交人工处理。
    """
    password = None
    if os.path.exists(ADMIN_FILE):
        password = Path(ADMIN_FILE).read_text().strip()
        saved = login(password)
        if saved:
            return saved
        if not _fresh_database():
            fail(
                "已保存的管理员口令登录失败，且默认口令也不可用："
                "该口令文件不是这台平台的口令，需要人工恢复管理员"
            )
        print("bootstrap: 库仍是默认状态，使用已持久保存的目标口令轮换", flush=True)
    legacy = login(DEFAULT_PASSWORD)
    if legacy is None:
        fail("默认口令已不可用但没有口令文件：无法继续（人工恢复）")
    if not password:
        fail("缺少宿主预先保存的目标口令：拒绝轮换")
    status, body = call(
        "/user/password",
        {"oldPassword": DEFAULT_PASSWORD, "newPassword": password},
        {"Snail-Ai-Auth": legacy},
        "PUT",
    )
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"修改默认管理员口令失败: {status} {body}")
    if login(DEFAULT_PASSWORD) is not None:
        fail("改口令之后默认口令仍然能登录")
    with open(ADMIN_FILE, "w") as handle:
        handle.write(password + "\n")
    os.chmod(ADMIN_FILE, 0o600)
    return admin_token() if login(password) else fail("新口令登录失败")


def disable_demo_app(token: str) -> None:
    status, body = call("/app/page?page=1&size=50", None, {"Snail-Ai-Auth": token}, "GET")
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"读取应用列表失败: {status}")
    for app in body["data"]:
        if app["appId"] == DEMO_APP and app.get("status") == 1:
            status, _ = call(f"/app/{app['id']}/toggle-status", {}, {"Snail-Ai-Auth": token}, "POST")
            if status != 200:
                fail(f"禁用 demo 应用失败: {status}")
            print("bootstrap: demo 应用已禁用")
            return
    print("bootstrap: demo 应用已是禁用状态")


def ensure_app(token: str) -> tuple[str, dict]:
    status, body = call("/app/page?page=1&size=50", None, {"Snail-Ai-Auth": token}, "GET")
    apps = {app["appId"]: app for app in (body or {}).get("data", [])}
    if SERVICE in apps:
        return apps[SERVICE]["token"], apps[SERVICE]
    status, body = call(
        "/app",
        {
            "appId": SERVICE,
            "appName": SERVICE,
            "description": "M2-LIVE-03 trial",
            "routeStrategy": "LEAST_LOAD",
        },
        {"Snail-Ai-Auth": token},
    )
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"创建应用失败: {status} {body}")
    return body["data"]["token"], body["data"]


def model_api_key() -> str:
    """模型凭据只从容器内的 0600 文件读 —— 不进命令行、不进日志、不进报错正文。"""
    path = Path("/tmp/model_api_key")
    if not path.is_file():
        fail("缺少 /tmp/model_api_key（bootstrap.sh 没有传入模型凭据）")
    key = path.read_text().strip()
    if not key:
        fail("/tmp/model_api_key 为空")
    return key


def endpoint_matches(config: dict, endpoint: str) -> bool:
    """已有模型配置的端点必须与本次给出的一致。

    不一致说明这台平台上的模型指向别处（例如上一次留在合成桩上）。静默复用会让
    「跑的是哪个模型」变成解释不清的问题，所以宁可失败也不改别人已经写好的配置。
    """
    return str(config.get("apiEndpoint") or "").rstrip("/") == endpoint.rstrip("/")


def read_model_config(token: str, config_id: int) -> dict | None:
    """读回模型配置；**读不到**返回 None，与「读到了但没有 key」区分开。

    轮换之后的核对依赖这一次读取：把它和「字段为空」混为一谈，会让读回失败静默通过。
    """
    status, body = call(f"/ai-model/config/{config_id}", None, {"Snail-Ai-Auth": token}, "GET")
    if status != 200 or not body or body.get("status") != 1:
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


def rotate_model_key(token: str, config: dict, endpoint: str, api_key: str) -> None:
    """端点上一致、密钥可能变了：用本次凭据覆盖，再读回核对。

    平台读取接口返回的是**明文** key（其注释写「脱敏」，实现不是），所以能逐字比对；
    若将来换成脱敏值（含 `*`），比对跳过，但仍以本次凭据写入 —— 轮换本来就是要覆盖。
    """
    stored = read_model_config(token, config["id"])
    if stored is None:
        fail("读不到模型配置，无法确认当前凭据")
    current = stored.get("apiKey")
    if isinstance(current, str) and current and "*" not in current and current == api_key:
        return
    status, body = call(
        f"/ai-model/config/{config['id']}",
        {
            "providerId": config.get("providerId"),
            "modelName": config.get("modelName"),
            "modelKey": config.get("modelKey"),
            "modelType": config.get("modelType") or "CHAT",
            "adapterKey": config.get("adapterKey") or "openai-compatible",
            "apiKey": api_key,
            "apiEndpoint": endpoint,
            "isDefault": bool(config.get("isDefault", True)),
        },
        {"Snail-Ai-Auth": token},
        "PUT",
    )
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"轮换模型凭据失败（HTTP {status}）")
    after = read_model_config(token, config["id"])
    if after is None:
        fail("轮换后读不到模型配置，无法确认凭据已生效")
    rotated = after.get("apiKey")
    if not isinstance(rotated, str) or not rotated:
        fail("轮换后模型凭据为空，无法确认已生效")
    if "*" in rotated:
        # 脱敏值无法逐字核对：宁可失败并要求换用可验证的接口，也不报一个未经证实的成功。
        fail("平台返回的是脱敏凭据，无法验证轮换结果；需要可验证的读取接口或明确确认机制")
    if rotated != api_key:
        fail("写入后读回的模型凭据与本次不一致")


def ensure_model(token: str, endpoint: str, api_key: str, model_id: str) -> int:
    if not model_id:
        # 在**改平台状态之前**拒绝：否则会留下一个供应商不认识的 modelKey。
        fail("M2LIVE_MODEL_ID 未设置：正式模式必须显式给出供应商模型标识")
    status, body = call("/ai-model/by-type/CHAT", None, {"Snail-Ai-Auth": token}, "GET")
    for config in (body or {}).get("data", []) or []:
        if config.get("modelKey") not in {model_id, SERVICE}:
            continue
        if config.get("modelKey") == SERVICE:
            fail(
                "平台上已有以内部资源名作 modelKey 的模型配置（上一版遗留）："
                "供应商会收到一个不认识的模型标识。删除该配置后重跑"
            )
        if not endpoint_matches(config, endpoint):
            fail(
                "已有模型配置的端点与本次不一致："
                f"平台上是 {config.get('apiEndpoint')!r}，本次是 {endpoint!r}。"
                "不自动改写；确认后删除该配置或修正 M2LIVE_MODEL_ENDPOINT"
            )
        rotate_model_key(token, config, endpoint, api_key)
        return config["id"]
    status, providers = call("/ai-model/providers", None, {"Snail-Ai-Auth": token}, "GET")
    provider_id = next(p["id"] for p in providers["data"] if p["providerKey"] == "openai")
    status, body = call(
        "/ai-model/config",
        {
            "providerId": provider_id,
            # `modelName` 是平台里的显示名；`modelKey` 是**发给供应商的 model**。
            "modelName": f"{SERVICE} ({model_id})",
            "modelKey": model_id,
            "modelType": "CHAT",
            "adapterKey": "openai-compatible",
            # 平台用服务端配置的 key/iv 加密后存储；这里传的是真实凭据（来自 0600 文件）。
            "apiKey": api_key,
            "apiEndpoint": endpoint,
            "isDefault": True,
        },
        {"Snail-Ai-Auth": token},
    )
    if not (status == 200 and body and body.get("status") == 1):
        # 不转述正文：它可能回显被拒的请求。
        fail(f"创建模型配置失败（HTTP {status}）")
    return body["data"]


#: 平台派发时下发的 system prompt 就来自这个 instruction（`SystemPromptHandler` 的字节码
#: 实测：`context.setSystemPrompt(agent.getInstruction())`，空则用一句默认）。
#: 因此**清单里的 system_messages 必须与它逐字相同** —— 这不是巧合，而是同一份值。
AGENT_INSTRUCTION = "用 charging_status_snapshot 工具取一次快照，并原样作答。"


#: 节点的非凭据开关：bootstrap 只重写凭据那三行，这些行必须原样保留。
NODE_ENV_PATH = "/tmp/node.env.in"
#: 输出到容器的临时文件；bootstrap.sh 之后把它发布到宿主。
NODE_ENV_OUT = "/tmp/node.env.out"


def _node_env_path() -> str:
    return NODE_ENV_PATH


def _node_env_out() -> str:
    return NODE_ENV_OUT


def _write_node_env(credentials: dict[str, str]) -> None:
    """把凭据写进 node.env，同时保留既有的非凭据键。

    读现有的 `$M2LIVE_CONFIG/node/node.env`（容器里是 `/etc/m2live/node.env`），
    覆盖同名键、追加新键；**已存在但不在凭据集合里的行逐字保留**。输出仍是 0600。
    """
    preserved: list[str] = []
    try:
        with open(_node_env_path(), encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    preserved.append(line.rstrip("\n"))
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key not in credentials:
                    preserved.append(line.rstrip("\n"))
    except FileNotFoundError:
        pass
    lines = preserved + [f"{key}={value}" for key, value in credentials.items()]
    with open(_node_env_out(), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(_node_env_out(), 0o600)


def ensure_agent(token: str, app_id: str, model_id: int) -> int:
    status, body = call("/agent/page?page=1&size=50", None, {"Snail-Ai-Auth": token}, "GET")
    for agent in (body or {}).get("data", []) or []:
        if agent.get("name") == SERVICE:
            return agent["id"]
    status, body = call(
        "/agent",
        {
            "name": SERVICE,
            "description": "M2-LIVE-03 trial agent",
            "instruction": AGENT_INSTRUCTION,
            "chatModelId": model_id,
            "appId": app_id,
            "mcpEnabled": False,
            "skillEnabled": False,
            "memoryEnabled": False,
            "webSearchEnabled": False,
            "ragEnabled": False,
        },
        {"Snail-Ai-Auth": token},
    )
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"创建智能体失败: {status} {body}")
    return body["data"]["id"]


def ensure_openapi_user(app_id: str, app_token: str, agent_id: int) -> str:
    headers = {"Snail-Ai-App-Id": app_id, "Snail-Ai-Token": app_token}
    status, body = call(
        "/openapi/v1/user/register",
        {"externalId": SERVICE, "nickname": "trial"},
        headers,
    )
    if not (status == 200 and body and body.get("status") == 1):
        fail(f"注册 OpenAPI 用户失败: {status} {body}")
    open_id = body["data"]["openId"]
    status, body = call("/openapi/v1/user/agent", {"openId": open_id, "agentId": agent_id}, headers)
    # 重复订阅返回非 1 也算已达成目标；只有「用户/智能体不存在」才是失败。
    if status != 200:
        fail(f"订阅智能体失败: {status} {body}")
    return open_id


def node_token() -> str:
    """节点凭据由 bootstrap.sh 写进容器内的 0600 文件，不从命令行参数来。"""
    path = Path("/tmp/node_token")
    if not path.is_file():
        fail("缺少 /tmp/node_token（bootstrap.sh 没有传入节点凭据）")
    token = path.read_text().strip()
    if not token:
        fail("/tmp/node_token 为空")
    return token


def bind_expected_node(bindings: dict, token: str) -> dict:
    """把**本次实际使用的**节点凭据摘要写进绑定表。

    Gateway 只认 `nodes[*].token_sha256`，节点只认 `node.env` 里的明文；两者不同步时
    节点的心跳与领取会一直被拒。所以摘要只能从同一个令牌派生，而不是沿用文件里的旧值。
    预期节点之外的其他节点条目保留（撤销/轮换由运维改这张表）。
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    nodes = [
        item
        for item in bindings.get("nodes", [])
        if isinstance(item, dict) and item.get("node_id") != EXPECTED_NODE
    ]
    nodes.append({"node_id": EXPECTED_NODE, "environment": ENVIRONMENT, "token_sha256": digest})
    bindings["nodes"] = nodes
    return bindings


def main() -> int:
    token = admin_token() if MODE == "check" else ensure_admin_rotated()

    if MODE == "check":
        if login(DEFAULT_PASSWORD) is not None:
            fail("默认管理员口令仍可登录")
        status, body = call("/app/page?page=1&size=50", None, {"Snail-Ai-Auth": token}, "GET")
        demo = [a for a in (body or {}).get("data", []) if a["appId"] == DEMO_APP and a.get("status") == 1]
        if demo:
            fail("demo 应用仍处于启用状态")
        print("bootstrap: 默认管理员口令与 demo 应用都已不可用")
        return 0

    disable_demo_app(token)
    app_token, app = ensure_app(token)
    # 正式栈**必须**给出真实模型端点：合成桩的默认值只属于 compose.synthetic.yaml。
    endpoint = os.environ.get("M2LIVE_MODEL_ENDPOINT", "").strip()
    synthetic = os.environ.get("M2LIVE_SYNTH_MODE") == "1"
    if not endpoint and synthetic:
        endpoint = "http://modelstub:9100/v1"
    if not endpoint:
        fail("M2LIVE_MODEL_ENDPOINT 未设置：正式试运行不接受合成模型端点")
    # 正式模式必须拿到真实模型凭据；合成模式用桩端点，凭据同样走 0600 文件
    # （由 bootstrap.sh 写入一个占位值），保证「凭据不经过命令行」这条对两种模式一致。
    model_id = ensure_model(token, endpoint, model_api_key(), MODEL_ID)
    agent_id = ensure_agent(token, app["appId"], model_id)
    open_id = ensure_openapi_user(app["appId"], app_token, agent_id)

    bindings = json.loads(Path(BINDINGS_IN).read_text())
    token_for_node = node_token()
    bindings.update(
        {
            "environment": ENVIRONMENT,
            "snail_base_url": os.environ.get("M2LIVE_PLATFORM_URL", "https://platform:8900"),
            "snail_app_id": app["appId"],
            "snail_app_token": app_token,
            "snail_agent_id": agent_id,
            "snail_open_id": open_id,
            "expected_node_id": EXPECTED_NODE,
        }
    )
    bind_expected_node(bindings, token_for_node)
    with open(BINDINGS_OUT, "w") as handle:
        json.dump(bindings, handle, indent=2)
    os.chmod(BINDINGS_OUT, 0o600)
    # 节点的平台应用凭据与绑定表必须同时更新；只改一边会让心跳在下一次重启时失效。
    #
    # **只重写三个凭据键，其余键原样保留**：这个文件同时承载非凭据开关（例如 M3 的
    # `AIOPS_EXEC_MANIFEST`）。早先整份覆盖会让那些开关在 bootstrap 之后静默消失 ——
    # 守卫看起来配了、实际已关。
    _write_node_env(
        {"SNAIL_AI_APP_ID": app["appId"], "SNAIL_AI_TOKEN": app_token, "AIOPS_NODE_TOKEN": token_for_node}
    )
    print(
        json.dumps(
            {
                "app_id": app["appId"],
                "agent_id": agent_id,
                "open_id_len": len(open_id),
                "default_admin_disabled": True,
                "demo_app_disabled": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
