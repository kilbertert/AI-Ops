"""来源凭据合同：错环境/请求改写/并发重放必须在用户业务处理前被拒。"""

import base64
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aiops_diagnostics.identity.gateway_admission import Admission, GatewayAdmission, request_digest


def encoded(body):
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=").decode()


@pytest.fixture
def fixture(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps(
            {
                "environment": "env-a",
                "issuer": "company-env-a",
                "keys": {
                    "key-1": key.public_key()
                    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                    .decode()
                },
            }
        )
    )
    path.chmod(0o600)
    admission = Admission(path, "env-a", tmp_path / "private" / "replay.db")
    app = FastAPI()
    seen = []

    @app.post("/v1/assistant/questions")
    async def ask(request: Request):
        body = await request.body()
        seen.append(body)
        return {"accepted": True}

    app.add_middleware(GatewayAdmission, admission=admission)
    scope = {
        "method": "POST",
        "path": "/v1/assistant/questions",
        "raw_path": b"/v1/assistant/questions",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer real-user"), (b"x-business-entry", b"operator")],
    }
    body = b'{"question":"test"}'

    def sign(changes=None, header=None):
        now = int(time.time())
        claims = dict(
            iss="company-env-a",
            aud="aiops-gateway",
            environment="env-a",
            iat=now,
            exp=now + 120,
            jti=uuid.uuid4().hex,
            request_sha256=request_digest(scope, body),
        )
        claims.update(changes or {})
        h = encoded(header or {"alg": "RS256", "typ": "aiops-gateway+jwt", "kid": "key-1"})
        p = encoded(claims)
        sig = key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())
        return h + "." + p + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    headers = {"Authorization": "Bearer real-user", "X-Business-Entry": "operator"}
    with TestClient(app) as client:
        yield client, admission, sign, headers, body, seen, path


def test_accepts_original_body_once_and_persists_replay(fixture):
    client, admission, sign, headers, body, seen, path = fixture
    token = sign()
    headers["X-AIOps-Gateway-Assertion"] = token
    assert client.post("/v1/assistant/questions", headers=headers, content=body).status_code == 200
    assert seen == [body]
    assert client.post("/v1/assistant/questions", headers=headers, content=body).status_code == 401
    reopened = Admission(path, "env-a", admission.database)
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    with pytest.raises(sqlite3.IntegrityError):
        reopened.verify(token, claims["request_sha256"])


@pytest.mark.parametrize(
    "claims",
    [
        {"environment": "env-b"},
        {"iss": "foreign"},
        {"aud": "other"},
        {"exp": 0},
        {"iat": True},
        {"exp": int(time.time()) + 10000},
        {"jti": "not-a-nonce"},
    ],
)
def test_bad_claims_never_enter_business(fixture, claims):
    client, _, sign, headers, body, seen, _ = fixture
    headers["X-AIOps-Gateway-Assertion"] = sign(claims)
    assert client.post("/v1/assistant/questions", headers=headers, content=body).status_code == 401
    assert seen == []


@pytest.mark.parametrize(
    "change",
    ["body", "query", "bearer", "entry", "source", "content-type", "duplicate", "signature", "missing"],
)
def test_request_tampering_rejected(fixture, change):
    client, _, sign, headers, body, seen, _ = fixture
    headers["X-AIOps-Gateway-Assertion"] = sign()
    url = "/v1/assistant/questions"
    if change == "body":
        body += b" "
    if change == "query":
        url += "?other=1"
    if change == "bearer":
        headers["Authorization"] = "Bearer foreign-user"
    if change == "entry":
        headers["X-Business-Entry"] = "consumer"
    if change == "content-type":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if change == "source":
        headers["X-AIOps-Source-Key"] = "different-path"
    if change == "signature":
        headers["X-AIOps-Gateway-Assertion"] += "x"
    if change == "missing":
        del headers["X-AIOps-Gateway-Assertion"]
    raw = list(headers.items())
    if change == "duplicate":
        raw.append(("Authorization", "Bearer second"))
    assert client.post(url, headers=raw, content=body).status_code == 401
    assert seen == []


def test_concurrent_replay_has_one_winner(fixture):
    _, admission, sign, _, _, _, _ = fixture
    token = sign()
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))

    def verify(_):
        try:
            admission.verify(token, claims["request_sha256"])
            return True
        except sqlite3.IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(verify, range(8))) == 1


def test_real_gateway_requires_assertion_before_existing_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from aiops_diagnostics.gateway.environment_registry import apply
    from aiops_diagnostics.gateway.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.identity.caller_auth import CallerAuthError

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = tmp_path / "server.env"
    config.write_text("# isolated")
    config.chmod(0o600)
    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "environment": "a",
                "issuer": "issuer-a",
                "keys": {
                    "key-1": key.public_key()
                    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                    .decode()
                },
            }
        )
    )
    trust.chmod(0o600)
    registry = tmp_path / "registry" / "catalogue.json"
    apply(
        registry,
        [
            dict(
                id="a",
                enabled=True,
                gateway_url="https://a.test",
                cpms_base_url="https://a.test/cp",
                config_ref=str(config),
                tenants=[],
            )
        ],
        expected_revision=0,
        actor="fixture",
    )
    settings = GatewayServerSettings(
        data_home=tmp_path / "state",
        database_file=tmp_path / "state" / "gateway.db",
        server_config_file=config,
        environment="a",
        environment_registry_file=str(registry),
        company_gateway_trust_file=str(trust),
    )
    calls = []

    class Resolver:
        def resolve(self, *args, **kw):
            calls.append(True)
            raise CallerAuthError("fixture user unauthorized", code="caller_auth.invalid")

    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        caller_resolver=Resolver(),
    )
    with TestClient(app) as client:
        assert (
            client.get("/v1/orders/example/access", headers={"Authorization": "Bearer user"}).json()["error"][
                "code"
            ]
            == "GATEWAY_ASSERTION_REJECTED"
        )
        assert calls == []
        scope = {
            "method": "GET",
            "path": "/v1/orders/example/access",
            "raw_path": b"/v1/orders/example/access",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer user")],
        }
        now = int(time.time())
        h = encoded({"alg": "RS256", "typ": "aiops-gateway+jwt", "kid": "key-1"})
        p = encoded(
            dict(
                iss="issuer-a",
                aud="aiops-gateway",
                environment="a",
                iat=now,
                exp=now + 120,
                jti=uuid.uuid4().hex,
                request_sha256=request_digest(scope, b""),
            )
        )
        sig = (
            base64.urlsafe_b64encode(key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256()))
            .rstrip(b"=")
            .decode()
        )
        r = client.get(
            "/v1/orders/example/access",
            headers={"Authorization": "Bearer user", "X-AIOps-Gateway-Assertion": h + "." + p + "." + sig},
        )
        assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"
        assert calls == [True]
        assert client.get("/health").status_code == 200
        assert client.get("/v1/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "typ": "aiops-gateway+jwt", "kid": "key-1"},
        {"alg": "RS256", "typ": "JWT", "kid": "key-1"},
        {"alg": "RS256", "typ": "aiops-gateway+jwt", "kid": "unknown"},
        {"alg": "RS256", "typ": "aiops-gateway+jwt", "kid": "key-1", "jku": "https://evil.test"},
    ],
)
def test_algorithm_and_key_selection_are_fixed(fixture, header):
    client, _, sign, headers, body, seen, _ = fixture
    headers["X-AIOps-Gateway-Assertion"] = sign(header=header)
    assert client.post("/v1/assistant/questions", headers=headers, content=body).status_code == 401
    assert seen == []


def test_replay_store_unavailable_is_retryable_not_invalid_identity(fixture, monkeypatch):
    from contextlib import contextmanager

    client, admission, sign, headers, body, seen, _ = fixture

    @contextmanager
    def locked():
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(admission, "connection", locked)
    headers["X-AIOps-Gateway-Assertion"] = sign()
    response = client.post("/v1/assistant/questions", headers=headers, content=body)
    assert response.status_code == 503
    assert response.json()["error"] == {"code": "GATEWAY_ASSERTION_UNAVAILABLE", "retryable": True}
    assert seen == []
