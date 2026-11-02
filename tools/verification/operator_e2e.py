"""以**真实运营账号**驱动 41 生产 Gateway 的管家端验收。

方法说明见 `docs/agents/operator-e2e-real-account.md`；这里只讲怎么跑。

    python3 tools/verification/operator_e2e.py "帮我分析这笔单的电费2116050516255272961"

依赖：本机 `ddddocr`、能 ssh 到 `aiops-41`、库外加密清单里有 `account+password` 资产。
为什么分工：开发机到公网入口不通，41 能直达 api.mall；41 上没有 ddddocr。
所以取图在 41、OCR 在本机、登录与发问在 41，凭据经 ssh stdin 传入。

判据是 `agent_runs` 的 `order_no` / `chat_binding` / `state`，不是回答好不好看。
"""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import sys
from pathlib import Path

HOST = "aiops-41"
CPMS_HOST = "https://api.mall.qushiyun.com"
GATEWAY = "http://172.18.0.1:8788"
ASSET = "aiops-ulink-account-20261030"
SECRETS = Path.home() / ".config/dev-host/secrets-age"
TOKEN_FILE = Path("/tmp/.aiops_e2e_token")

#: 公司网关 `PasswordDecoderFilter` 的 security.encode.key（key == IV）。明文口令会 500。
AES_KEY = b"1234567891234567"


# ---------------------------------------------------------------- 身份


def load_asset(asset_id: str = ASSET) -> dict:
    out = subprocess.run(
        ["age", "-d", "-i", str(SECRETS / "key.txt"), str(SECRETS / "secrets.age")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("asset_id") == asset_id:
            return item
    raise SystemExit(f"asset {asset_id} not found in the encrypted inventory")


def encrypt_password(plain: str) -> str:
    """AES/CBC/ZeroPadding + base64。网关解密后 trim() 去掉填充的 0x00。"""
    from Crypto.Cipher import AES

    raw = plain.encode("utf-8")
    raw += b"\x00" * (16 - len(raw) % 16)
    return base64.b64encode(AES.new(AES_KEY, AES.MODE_CBC, AES_KEY).encrypt(raw)).decode("ascii")


def _ssh(script: str, payload: str | None = None, *, host: str = HOST, timeout: int = 900):
    proc = subprocess.run(
        ["ssh", host, script], input=payload, capture_output=True, text=True, timeout=timeout
    )
    return proc.stdout, proc.stderr, proc.returncode


def login(asset: dict) -> str:
    random_str = secrets.token_hex(6)[:11]
    grab = (
        "import base64, urllib.request\n"
        f"with urllib.request.urlopen('{CPMS_HOST}/code?randomStr={random_str}', timeout=15) as r:\n"
        "    print(base64.b64encode(r.read()).decode())\n"
    )
    grab_path = Path("/tmp/.aiops_e2e_captcha.py")
    grab_path.write_text(grab)
    grab_path.chmod(0o600)
    subprocess.run(["scp", "-q", str(grab_path), f"{HOST}:/tmp/.aiops_e2e_captcha.py"], check=True)
    out, err, rc = _ssh("python3 /tmp/.aiops_e2e_captcha.py")
    if rc != 0:
        raise SystemExit("captcha fetch failed: " + err[-400:])
    import ddddocr

    code = ddddocr.DdddOcr(show_ad=False).classification(base64.b64decode(out.strip()))
    payload = json.dumps(
        {
            "username": asset["username"],
            "password": encrypt_password(asset["password"]),
            "random_str": random_str,
            "code": code,
        }
    )
    driver = f'''
import json, sys, urllib.parse, urllib.request, urllib.error
c = json.load(sys.stdin)
q = urllib.parse.urlencode({{"username": c["username"], "password": c["password"],
    "randomStr": c["random_str"], "code": c["code"], "grant_type": "password",
    "scope": "server", "deviceType": "shop"}})
req = urllib.request.Request("{CPMS_HOST}/upms/token/login?" + q, method="POST",
                             headers={{"client-type": "admin"}})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(json.dumps({{"status": r.status, "body": r.read().decode()}}))
except urllib.error.HTTPError as e:
    print(json.dumps({{"status": e.code, "body": e.read().decode()}}))
'''
    # ssh 的 stdin 只有一份：驱动脚本先落盘再 scp，JSON 走 stdin。
    bound = Path("/tmp/.aiops_e2e_login.py")
    bound.write_text(driver)
    bound.chmod(0o600)
    subprocess.run(["scp", "-q", str(bound), f"{HOST}:/tmp/.aiops_e2e_login.py"], check=True)
    out, err, rc = _ssh("python3 /tmp/.aiops_e2e_login.py", payload=payload)
    if rc != 0:
        raise SystemExit("login failed: " + err[-400:])
    body = json.loads(json.loads(out.strip().splitlines()[-1])["body"])
    token = body.get("access_token")
    if not token:
        raise SystemExit(f"no access_token (msg={body.get('msg')!r})")
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


def source_key() -> str:
    script = (
        'PID=$(systemctl show aiops-gateway-41 -p MainPID --value); echo "$PID"; '
        'tr "\\0" "\\n" < "/proc/$PID/environ" '
        '| grep "^AIOPS_GATEWAY_COMPANY_SOURCE_KEY=" | cut -d= -f2-'
    )
    out, err, rc = _ssh("sudo -n bash -s", payload=script, timeout=120)
    lines = [line for line in out.strip().splitlines() if line]
    if len(lines) < 2 or not lines[-1]:
        raise SystemExit("cannot read source key: " + (err[-300:] or out[-200:]))
    return lines[-1]


# ---------------------------------------------------------------- 发问

ASK = r"""
import json, sys, time, urllib.request, urllib.error
d = json.load(sys.stdin)
headers = {"Authorization": "Bearer " + d["token"], "X-Business-Entry": "operator",
           "X-AIOps-Source-Key": d["source_key"],
           "Content-Type": "application/json", "Accept": "application/json"}
body = json.dumps({"question": d["question"]}).encode()
req = urllib.request.Request(d["base"] + "/v1/assistant/questions", data=body,
                             headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        status, text = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    status, text = e.code, e.read().decode()
print("POST", status, text[:400])
try:
    qa = json.loads(text).get("qa_id")
except Exception:
    sys.exit(0)
if not qa:
    sys.exit(0)
for _ in range(150):
    g = urllib.request.Request(d["base"] + "/v1/assistant/questions/" + qa, headers=headers)
    try:
        with urllib.request.urlopen(g, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        payload = {"status": "http_%d" % e.code}
    if payload.get("status") not in ("queued", "running"):
        break
    time.sleep(2)
print("QA_ID", qa)
print("FINAL", payload.get("status"))
for b in (payload.get("result") or {}).get("blocks") or []:
    if b.get("kind") == "text":
        print("ANSWER_BEGIN"); print(b.get("text", "")[:4000]); print("ANSWER_END")
print("ERROR", json.dumps(payload.get("error"), ensure_ascii=False))
"""

ROUTE = """
import json, sqlite3, sys
qa = sys.argv[1]
db = sqlite3.connect("/var/lib/aiops-41/gateway/gateway.db")
row = db.execute("SELECT run_id, order_no, state, chat_binding, exec_config_state, "
                 "route_revision, scope_json FROM agent_runs WHERE qa_id = ?", (qa,)).fetchone()
if not row:
    print(json.dumps({"found": False}))
else:
    scope = json.loads(row[6] or "{}")
    print(json.dumps({"found": True, "run_id": row[0], "order_no": row[1], "state": row[2],
                      "chat_binding": row[3], "exec_config_state": row[4],
                      "route_revision": row[5], "tenant": scope.get("effective_tenant_id"),
                      "username": (scope.get("caller") or {}).get("username")},
                     ensure_ascii=False))
"""


def ask(token: str, key: str, question: str) -> str | None:
    for name, body in ((".aiops_e2e_ask.py", ASK), (".aiops_e2e_route.py", ROUTE)):
        path = Path("/tmp") / name
        path.write_text(body)
        path.chmod(0o600)
        subprocess.run(["scp", "-q", str(path), f"{HOST}:/tmp/{name}"], check=True)
    payload = json.dumps({"base": GATEWAY, "token": token, "source_key": key, "question": question})
    out, err, rc = _ssh("python3 /tmp/.aiops_e2e_ask.py", payload=payload)
    print(out.strip()[:5000])
    if rc != 0:
        print("stderr:", err[-400:])
        return None
    return next((line.split(None, 1)[1] for line in out.splitlines() if line.startswith("QA_ID ")), None)


def route(qa: str) -> None:
    # 路由脚本放在配置根，由服务身份执行；`/tmp` 下 claude 写的文件 aiops41 读不到。
    out, err, rc = _ssh(
        "sudo -n install -m 644 -o aiops41 -g aiops41 /tmp/.aiops_e2e_route.py /etc/aiops-41/route.py "
        f"&& sudo -n -u aiops41 /opt/aiops-41/.venv/bin/python /etc/aiops-41/route.py {qa} "
        "&& sudo -n rm -f /etc/aiops-41/route.py"
    )
    print("ROUTE", out.strip()[:800] or err[-300:])


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    token = login(load_asset())
    key = source_key()
    for i, question in enumerate(argv, 1):
        print(f"===== Q{i}: {question}")
        qa = ask(token, key, question)
        if qa:
            route(qa)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
