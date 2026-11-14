"""平台环境/租户登记；每次准入读取原子发布文件，不猜默认环境。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint
from aiops_diagnostics.common.private_files import (
    PrivatePathError,
    ensure_private_directory,
    validate_private_directory,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc, parse_stored_time
from aiops_diagnostics.gateway.deployment_environment import canonical_environment

SCHEMA = "aiops-environment-registry/1"
MANAGED_SCHEMA = "aiops-environment-registry/2"
MAX_BYTES = 8 * 1024 * 1024
ACTOR_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,128}")


class RegistryError(ValueError):
    pass


def validate(rows: object, *, managed: bool = False) -> list[dict]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise RegistryError("环境清单须包含1至64个环境")
    identifiers, endpoints = set(), set()
    for row in rows:
        fields = {"id", "enabled", "gateway_url", "cpms_base_url", "config_ref", "tenants"}
        if managed:
            fields |= {"name", "company_gateway_url"}
        if not isinstance(row, dict) or set(row) != fields:
            raise RegistryError("环境登记字段不完整或含未知字段")
        identifier = row["id"]
        if not isinstance(identifier, str) or canonical_environment(identifier) != identifier:
            raise RegistryError("环境编号不规范")
        if identifier in identifiers or type(row["enabled"]) is not bool:
            raise RegistryError("环境编号重复或启用状态无效")
        identifiers.add(identifier)
        for key in ("gateway_url", "cpms_base_url"):
            value = row[key]
            if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
                raise RegistryError("业务地址无效")
            from urllib.parse import urlsplit

            if urlsplit(value).port == 0:
                raise RegistryError("业务地址端口不能为0")
            if require_secure_endpoint(value, what=key) != value:
                raise RegistryError("业务地址必须使用规范形式")
        parsed = urlsplit(row["gateway_url"])
        endpoint = (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
        if parsed.path or parsed.port == 0 or endpoint in endpoints:
            raise RegistryError("环境Gateway须独立且不含路径")
        endpoints.add(endpoint)
        if not isinstance(row["config_ref"], str) or not Path(row["config_ref"]).is_absolute():
            raise RegistryError("环境配置引用须为绝对路径")
        if managed:
            _display_name(row["name"])
            endpoint = row["company_gateway_url"]
            if endpoint:
                from urllib.parse import urlsplit

                if (
                    not isinstance(endpoint, str)
                    or urlsplit(endpoint).scheme != "https"
                    or any(c.isspace() or ord(c) < 32 for c in endpoint)
                    or require_secure_endpoint(endpoint, what="公司网关") != endpoint
                ):
                    raise RegistryError("公司网关须为规范HTTPS地址")
            elif not isinstance(endpoint, str):
                raise RegistryError("公司网关地址无效")
        tenants = row["tenants"]
        if not isinstance(tenants, list) or len(tenants) > 10000:
            raise RegistryError("租户清单无效")
        seen = set()
        for tenant in tenants:
            tenant_fields = {"id", "enabled", "name"} if managed else {"id", "enabled"}
            if not isinstance(tenant, dict) or set(tenant) != tenant_fields:
                raise RegistryError("租户登记字段无效")
            tid = tenant["id"]
            if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
                raise RegistryError("租户编号无效")
            if tid in seen or type(tenant["enabled"]) is not bool:
                raise RegistryError("租户编号重复或启用状态无效")
            if managed:
                _display_name(tenant["name"])
            seen.add(tid)
    return rows


def _display_name(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise RegistryError("名称须为1至128个可显示字符")


def validate_managers(managers, rows):
    if not isinstance(managers, list) or len(managers) > 256:
        raise RegistryError("环境管理授权无效")
    environments, seen = {r["id"] for r in rows}, set()
    for manager in managers:
        if not isinstance(manager, dict) or set(manager) != {
            "environment",
            "tenant_id",
            "user_id",
            "environments",
        }:
            raise RegistryError("环境管理授权字段无效")
        for key in ("environment", "tenant_id", "user_id"):
            if not isinstance(manager[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", manager[key]):
                raise RegistryError("环境管理主体无效")
        scope = manager["environments"]
        if (
            manager["environment"] not in environments
            or not isinstance(scope, list)
            or not scope
            or any(not isinstance(v, str) or v not in environments for v in scope)
            or len(scope) != len(set(scope))
        ):
            raise RegistryError("环境管理范围无效")
        identity = tuple(manager[k] for k in ("environment", "tenant_id", "user_id"))
        if identity in seen:
            raise RegistryError("环境管理主体重复")
        seen.add(identity)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("登记包含重复字段")
        result[key] = value
    return result


def load(path: str | Path) -> dict:
    try:
        path = Path(path)
        if any(parent.is_symlink() for parent in path.absolute().parents):
            raise RegistryError("登记路径包含符号链接目录")
        validate_private_directory(path.parent)
        validate_private_file(path)
        if path.stat().st_size > MAX_BYTES:
            raise RegistryError("登记文件超过大小上限")
        body = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(body, dict):
            raise RegistryError("登记文件形状错误")
        managed = body.get("schema") == MANAGED_SCHEMA
        fields = {"schema", "revision", "environments", "audit"} | ({"managers"} if managed else set())
        if set(body) != fields:
            raise RegistryError("登记文件形状错误")
        if (
            body["schema"] not in (SCHEMA, MANAGED_SCHEMA)
            or type(body["revision"]) is not int
            or body["revision"] < 1
        ):
            raise RegistryError("登记版本无效")
        if not isinstance(body["audit"], list) or len(body["audit"]) != body["revision"]:
            raise RegistryError("登记审计不完整")
        for revision, record in enumerate(body["audit"], 1):
            if (
                not isinstance(record, dict)
                or set(record) != {"revision", "actor", "at"}
                or type(record["revision"]) is not int
                or record["revision"] != revision
            ):
                raise RegistryError("登记审计版本错误")
            if not isinstance(record["actor"], str) or not ACTOR_PATTERN.fullmatch(record["actor"]):
                raise RegistryError("登记审计操作人错误")
            if (
                not isinstance(record["at"], str)
                or format_stored_time(parse_stored_time(record["at"])) != record["at"]
            ):
                raise RegistryError("登记审计时间错误")
        validate(body["environments"], managed=managed)
        if managed:
            validate_managers(body["managers"], body["environments"])
        return body
    except (OSError, ValueError, TypeError, PrivatePathError) as exc:
        raise RegistryError("ENVIRONMENT_REGISTRY_UNAVAILABLE") from exc


def apply(path: Path, rows: list[dict], *, expected_revision: int, actor: str, managers=None) -> int:
    import fcntl
    import os

    validate(rows, managed=managers is not None)
    if managers is not None:
        validate_managers(managers, rows)
    if type(expected_revision) is not int or expected_revision < 0:
        raise RegistryError("登记预期版本必须是非负整数")
    if not isinstance(actor, str) or not ACTOR_PATTERN.fullmatch(actor):
        raise RegistryError("操作人标识无效")
    path = path.absolute()
    # 专用私有目录，锁覆盖读取、比较版本和原子替换，避免并发丢失更新。
    if any(parent.is_symlink() for parent in path.parents):
        raise RegistryError("登记路径包含符号链接目录")
    if path.parent.exists():
        validate_private_directory(path.parent)
    else:
        ensure_private_directory(path.parent)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load(path) if path.exists() or path.is_symlink() else {"revision": 0, "audit": []}
        if old["revision"] != expected_revision:
            raise RegistryError("ENVIRONMENT_REGISTRY_REVISION_CONFLICT")
        if old.get("schema") == MANAGED_SCHEMA and managers is None:
            raise RegistryError("ENVIRONMENT_REGISTRY_DOWNGRADE_REFUSED")
        revision = expected_revision + 1
        body = {
            "schema": MANAGED_SCHEMA if managers is not None else SCHEMA,
            "revision": revision,
            "environments": rows,
            "audit": [
                *old["audit"],
                {"revision": revision, "actor": actor, "at": format_stored_time(now_utc())},
            ],
        }
        if managers is not None:
            body["managers"] = managers
        content = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        if len(content.encode()) > MAX_BYTES:
            raise RegistryError("登记文件超过大小上限")
        write_private_text(path, content)
        return revision


class EnvironmentRegistry:
    def __init__(self, path: str | Path, environment: str):
        self.path = path
        self.environment = canonical_environment(environment)
        self._configuration = None
        row = self.entry()
        self._configuration = (row["gateway_url"], row["cpms_base_url"], row["config_ref"])

    def entry(self) -> dict:
        row = next((r for r in load(self.path)["environments"] if r["id"] == self.environment), None)
        if row is None:
            raise RegistryError("ENVIRONMENT_NOT_REGISTERED")
        if self._configuration is not None and self._configuration != (
            row["gateway_url"],
            row["cpms_base_url"],
            row["config_ref"],
        ):
            raise RegistryError("ENVIRONMENT_CONFIGURATION_CHANGED")
        return row

    def require(self, tenant_id: str) -> dict:
        row = self.entry()
        if not row["enabled"] or not any(t["id"] == tenant_id and t["enabled"] for t in row["tenants"]):
            raise RegistryError("TENANT_ENVIRONMENT_NOT_ENABLED")
        return row


def require_registered(runtime, tenant_id: str):
    """复用启动时冻结的环境连接，工具与后台交付不能重新选址。"""
    if not getattr(getattr(runtime, "gateway_settings", None), "environment_registry_file", ""):
        return None
    registry = getattr(runtime, "environment_registry", None)
    if registry is None:
        raise RegistryError("ENVIRONMENT_REGISTRY_UNAVAILABLE")
    return registry.require(tenant_id)


def for_settings(settings):
    """每个设置实例只绑定一次连接；租户启停仍逐次读取当前登记。"""
    registry = getattr(settings, "_environment_registry", None)
    if registry is None:
        registry = EnvironmentRegistry(settings.environment_registry_file, settings.environment)
        settings._environment_registry = registry
    return registry
