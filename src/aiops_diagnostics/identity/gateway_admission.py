"""公司网关来源证明：固定RS256、环境与请求绑定、持久防重放；不替代用户授权。"""

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.responses import JSONResponse

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    protect_private_file,
    validate_private_directory,
    validate_private_file,
)
from aiops_diagnostics.gateway import gateway_db

HEADER = b"x-aiops-gateway-assertion"
MAX_BODY = 8 * 1024 * 1024


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _decode(value):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid encoding")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("noncanonical encoding")
    return raw


def request_digest(scope, body):
    """签发/验证共用协议：按原字节保留路径/query，身份头重复即拒绝。"""
    headers = {}
    for key, value in scope["headers"]:
        key = key.lower()
        if key in (
            b"authorization",
            b"x-third-session",
            b"x-business-entry",
            b"x-aiops-source-key",
            b"content-type",
        ):
            if key in headers:
                raise ValueError("duplicate identity header")
            headers[key] = value.decode("latin1")
    fields = [
        scope["method"].upper(),
        scope.get("raw_path", scope["path"].encode()).decode("ascii"),
        scope.get("query_string", b"").decode("ascii"),
        hashlib.sha256(body).hexdigest(),
        headers.get(b"authorization", ""),
        headers.get(b"x-third-session", ""),
        headers.get(b"x-business-entry", ""),
        headers.get(b"x-aiops-source-key", ""),
        headers.get(b"content-type", ""),
    ]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class Admission:
    def __init__(self, config_file, environment, database):
        path = Path(config_file)
        validate_private_file(path)
        if path.stat().st_size > 65536:
            raise ValueError("gateway admission config too large")
        config = json.loads(path.read_text(), object_pairs_hook=_object)
        if (
            not isinstance(config, dict)
            or set(config) != {"environment", "issuer", "keys"}
            or config["environment"] != environment
        ):
            raise ValueError("gateway admission environment mismatch")
        if not isinstance(config["issuer"], str) or not 1 <= len(config["issuer"]) <= 256:
            raise ValueError("gateway admission issuer invalid")
        if not isinstance(config["keys"], dict) or not 1 <= len(config["keys"]) <= 4:
            raise ValueError("gateway admission keys invalid")
        self.keys = {}
        for kid, pem in config["keys"].items():
            if not re.fullmatch(r"[a-z0-9-]{1,64}", kid) or not isinstance(pem, str):
                raise ValueError("gateway admission key invalid")
            key = serialization.load_pem_public_key(pem.encode())
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                raise ValueError("gateway admission requires RSA >= 2048")
            self.keys[kid] = key
        self.environment, self.issuer = environment, config["issuer"]
        self.database = Path(database)
        if any(parent.is_symlink() for parent in self.database.absolute().parents):
            raise ValueError("replay database parent cannot be a symlink")
        if self.database.parent.exists():
            validate_private_directory(self.database.parent)
        else:
            ensure_private_directory(self.database.parent)
        if self.database.exists() or self.database.is_symlink():
            validate_private_file(self.database)
        try:
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            validate_private_file(self.database)
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS replays "
                "(issuer TEXT, jti TEXT, expires INTEGER, PRIMARY KEY(issuer,jti))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS replay_expiry ON replays(expires)")
        protect_private_file(self.database)

    def connection(self):
        return gateway_db.connection(self.database, write=True)

    def verify(self, token, digest):
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError("invalid assertion")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid assertion")
        header = json.loads(_decode(parts[0]), object_pairs_hook=_object)
        if not isinstance(header, dict) or set(header) != {"alg", "typ", "kid"}:
            raise ValueError("invalid header")
        if header["alg"] != "RS256" or header["typ"] != "aiops-gateway+jwt":
            raise ValueError("invalid algorithm")
        key = self.keys.get(header["kid"]) if isinstance(header["kid"], str) else None
        if key is None:
            raise ValueError("unknown key")
        key.verify(
            _decode(parts[2]), (parts[0] + "." + parts[1]).encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        claims = json.loads(_decode(parts[1]), object_pairs_hook=_object)
        required = {"iss", "aud", "environment", "iat", "exp", "jti", "request_sha256"}
        if not isinstance(claims, dict) or set(claims) != required:
            raise ValueError("invalid claims")
        now = int(time.time())
        if (
            claims["iss"] != self.issuer
            or claims["environment"] != self.environment
            or claims["aud"] != "aiops-gateway"
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not now - 150 <= claims["iat"] <= now + 30
            or not now < claims["exp"] <= claims["iat"] + 120
            or not claims["iat"] < claims["exp"]
            or claims["request_sha256"] != digest
            or not isinstance(claims["jti"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", claims["jti"])
        ):
            raise ValueError("invalid claim binding")
        with self.connection() as db:
            db.execute("DELETE FROM replays WHERE expires<=?", (now,))
            if db.execute("SELECT count(*) FROM replays").fetchone()[0] >= 100000:
                raise ValueError("replay capacity exceeded")
            db.execute("INSERT INTO replays VALUES(?,?,?)", (self.issuer, claims["jti"], claims["exp"]))


class GatewayAdmission:
    def __init__(self, app, admission):
        self.app, self.admission = app, admission

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        # 内部回调与Dify管理仍保留各自认证；公开用户API必须来源证明。
        if (
            scope["type"] != "http"
            or not path.startswith("/v1/")
            or path.startswith(("/v1/internal/", "/v1/dify/"))
            or path == "/v1/openapi.json"
        ):
            return await self.app(scope, receive, send)
        try:
            values = [v for k, v in scope["headers"] if k.lower() == HEADER]
            if len(values) != 1:
                raise ValueError("assertion required")
            chunks, size = [], 0
            deadline = time.monotonic() + 15
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("request body deadline exceeded")
                event = await asyncio.wait_for(receive(), max(0.001, deadline - time.monotonic()))
                if event["type"] != "http.request":
                    return
                chunk = event.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    raise ValueError("body too large")
                if chunk:
                    chunks.append(chunk)
                if not event.get("more_body", False):
                    break
            body = b"".join(chunks)
            await asyncio.to_thread(
                self.admission.verify, values[0].decode("ascii"), request_digest(scope, body)
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            TimeoutError,
            RecursionError,
            InvalidSignature,
            sqlite3.Error,
        ):
            response = JSONResponse(
                {"error": {"code": "GATEWAY_ASSERTION_REJECTED", "retryable": False}},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
