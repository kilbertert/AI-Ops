"""停用客服候选的单轮能力；公司会话仅保存在目标进程内存。"""

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.gateway import environment_registry
from aiops_diagnostics.identity.third_session_auth import RedisThirdSessionResolver, ThirdSessionSettings

PREFIX = "ect_"


async def callback(context, request, token):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    raw = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 6000:
                    return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, status_code=413)
        result = await run_in_threadpool(
            context.consumer_candidate_leases.call,
            token,
            json.loads(raw, object_pairs_hook=environment_registry._unique_object),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        return JSONResponse(
            {"error": {"code": "CANDIDATE_TRIAL_REFUSED"}},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )


def descriptor(value):
    fields = {
        "environment",
        "tenant_id",
        "workspace_id",
        "app_id",
        "knowledge_base_id",
        "task_id",
        "plan_id",
        "workflow_sha256",
        "order_no",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or any(
            not isinstance(v, str) or len(v) > 128 or (not v and k != "order_no") for k, v in value.items()
        )
    ):
        raise ValueError("invalid candidate scope")
    from aiops_diagnostics.identity.order_identity import is_order_identifier

    if value["order_no"] and not is_order_identifier(value["order_no"]):
        raise ValueError("invalid candidate order")
    if len(value["workflow_sha256"]) != 64 or any(
        c not in "0123456789abcdef" for c in value["workflow_sha256"]
    ):
        raise ValueError("invalid candidate workflow")
    if not re.fullmatch(r"task_[0-9a-f]{32}", value["task_id"]) or not re.fullmatch(
        r"plan_[0-9a-f]{32}", value["plan_id"]
    ):
        raise ValueError("invalid candidate task")
    return dict(value)


def installed(context, scope):
    """只接受原安装器的停用实例；不收养未知部署或更换数据源。"""
    settings = context.settings
    root = Path("/etc/aiops-access-" + settings.environment)
    application = Path("/opt/aiops-access-" + settings.environment)
    if (
        scope["environment"] != settings.environment
        or settings.execution_environment != settings.environment
        or Path(settings.server_config_file or "") != root / "production.env"
        or settings.environment_registry_file != str(root / "registry/environment.json")
        or (application / "pending.json").exists()
    ):
        raise ValueError("candidate installation unavailable")
    registry = environment_registry.load(settings.environment_registry_file)
    row = next((r for r in registry["environments"] if r["id"] == scope["environment"]), None)
    tenant = next((t for t in row["tenants"] if t["id"] == scope["tenant_id"]), None) if row else None
    if tenant is None:
        raise ValueError("candidate tenant not registered")
    build = json.loads((application / "current/manifest.json").read_bytes())
    if tenant["enabled"] or build["version"] != __import__("aiops_diagnostics").__version__:
        raise ValueError("candidate installation changed")
    return row["cpms_base_url"], tuple(
        hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            root / "production.env",
            root / "registry/environment.json",
            application / "current/manifest.json",
            root / "runtime/manifest.json",
        )
    )


def consumer_identity(context, session):
    settings = context.settings
    config = Settings.from_config(settings.server_config_file)
    return RedisThirdSessionResolver(
        ThirdSessionSettings(
            config.redis.host,
            config.redis.port,
            config.redis.database,
            username=config.redis.user,
            password=config.redis.password,
            service_token=settings.third_session_service_token,
            key_prefix=settings.third_session_key_prefix,
        )
    ).resolve(
        settings.third_session_service_token,
        required_scope="aiops:orders:read",
        third_session=session,
        platform_entry="consumer",
    )


@dataclass
class Lease:
    scope: dict
    session: str = field(repr=False)
    caller: object
    snapshot: tuple
    until: float
    state: str = "open"
    calls: int = 0
    lock: object = field(default_factory=threading.RLock, repr=False)


class CandidateLeases:
    def __init__(self, context):
        self.context, self.leases, self.lock = context, {}, threading.RLock()

    def open(self, payload):
        if set(payload) != {"candidate", "third_session"}:
            raise ValueError("invalid candidate opening")
        scope, session = descriptor(payload["candidate"]), payload["third_session"]
        if not isinstance(session, str) or not 1 <= len(session) <= 256:
            raise ValueError("consumer session required")
        caller = consumer_identity(self.context, session)
        snapshot = installed(self.context, scope)
        if (
            caller.effective_tenant_id != scope["tenant_id"]
            or caller.delegated
            or (caller.data_scope.type != "self" or not caller.subject.c_user_id)
        ):
            raise ValueError("candidate consumer identity mismatch")
        if scope["order_no"]:
            self.context.runtime._require_chat_order_visible(caller, scope["order_no"])
        with self.lock:
            self.leases = {k: v for k, v in self.leases.items() if v.until > time.monotonic()}
            if len(self.leases) >= 20:
                raise ValueError("candidate trials busy")
            token = PREFIX + secrets.token_urlsafe(32)
            identifier = hashlib.sha256(token.encode()).hexdigest()
            self.leases[identifier] = Lease(scope, session, caller, snapshot, time.monotonic() + 300)
            expiry = threading.Timer(300, self.forget, args=(identifier,))
            expiry.daemon = True
            expiry.start()
        return {
            "token": token,
            "scope_json": scope_to_json(caller),
            "candidate": scope,
            "scope": "candidate-only",
            "activated": False,
        }

    def forget(self, identifier):
        with self.lock:
            self.leases.pop(identifier, None)

    def check(self, lease):
        if time.monotonic() >= lease.until or installed(self.context, lease.scope) != lease.snapshot:
            raise ValueError("candidate expired or installation changed")
        fresh = consumer_identity(self.context, lease.session)
        if fresh.scope_fingerprint != lease.caller.scope_fingerprint:
            raise ValueError("candidate consumer changed")
        if lease.scope["order_no"]:
            self.context.runtime._require_chat_order_visible(fresh, lease.scope["order_no"])
        if time.monotonic() >= lease.until or installed(self.context, lease.scope) != lease.snapshot:
            raise ValueError("candidate expired or changed during authorization")
        return fresh

    def call(self, token, body):
        if not isinstance(body, dict) or set(body) - {"action", "candidate", "resource", "order_no"}:
            raise ValueError("invalid candidate request")
        with self.lock:
            lease = self.leases.get(hashlib.sha256(token.encode()).hexdigest())
        if lease is None or descriptor(body.get("candidate")) != lease.scope:
            raise ValueError("candidate not found")
        with lease.lock:
            action = body.get("action")
            if action == "close":
                # 撤销不依赖正向授权；身份失效也必须不可逆地关闭本轮能力。
                lease.state = "closed"
            caller = self.check(lease)
            if action == "claim":
                if lease.state != "open":
                    raise ValueError("candidate already used")
                lease.state = "claimed"
            elif action == "read":
                if lease.state != "claimed" or lease.calls >= 8:
                    raise ValueError("candidate reading closed")
                resource, order = body.get("resource"), body.get("order_no")
                if resource not in {"status", "billing"} or not order or order != lease.scope["order_no"]:
                    raise ValueError("candidate order not selected")
                lease.calls += 1
                base = lease.snapshot[0]
                args = {"tenant_id": caller.effective_tenant_id, "user_id": caller.subject.c_user_id}
                if resource == "status":
                    from aiops_diagnostics.data.charging_status import read_charging_status

                    result = read_charging_status(base, order, **args)
                else:
                    from aiops_diagnostics.consumer.consumer_billing import read_billing_record

                    result = read_billing_record(base, order, include_composition=True, **args)
                self.check(lease)
                return {"status": "found", "snapshot": result, "order_ids": [order]}
            elif action not in {"check", "close"}:
                raise ValueError("invalid candidate action")
            self.check(lease)
            return {
                "candidate": lease.scope,
                "scope_json": scope_to_json(caller),
                "state": lease.state,
                "scope": "candidate-only",
                "activated": False,
            }
