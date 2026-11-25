"""目录给地址，私有投影给凭据；活动登录选择环境，不向业务端广播会话。"""

import hashlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.gateway.deployment_environment import canonical_environment


def refused(code, message):
    from .core import PermissionDenied

    return PermissionDenied(code, message)


def credentials(path):
    path = Path(path)
    if any(parent.is_symlink() for parent in path.absolute().parents):
        raise ValueError("console credentials path contains symlinks")
    validate_private_file(path)
    if path.stat().st_size > 262144:
        raise ValueError("console credentials too large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate console credential field")
            result[key] = value
        return result

    body = json.loads(path.read_text(), object_pairs_hook=unique)
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "credentials"}
        or body["schema"] != "aiops-console-environments/1"
    ):
        raise ValueError("invalid console environment credentials")
    rows = body["credentials"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("invalid console credential count")
    result = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or canonical_environment(row["id"]) != row["id"]
            or row["id"] in result
        ):
            raise ValueError("invalid console credential identity")
        result[row["id"]] = row
    return result


class LoginEnvironments:
    def __init__(self, settings, default_service, build):
        from aiops_diagnostics.gateway.environment_registry import EnvironmentRegistry

        self.settings = settings
        self.entries = {}
        self.challenges = {}
        self.lock = threading.Lock()
        if not settings.environment_credentials_file:
            self.entries[settings.dify.environment] = (settings, default_service or build(settings), None)
            return
        if not settings.environment_registry_file:
            raise ValueError("multi-environment login requires a registry")
        roots = {Path(settings.data_dir).resolve()}
        keys = set()
        for identifier, row in credentials(settings.environment_credentials_file).items():
            if set(row) != {
                "id",
                "encode_key",
                "basic_auth",
                "client_type",
                "info_client_type",
                "gateway_internal_token",
                "operator_trial_key",
            }:
                raise ValueError("invalid login credential fields")
            if not all(isinstance(v, str) for v in row.values()):
                raise ValueError("invalid login credential values")
            registry = EnvironmentRegistry(settings.environment_registry_file, identifier)
            target = registry.entry()
            if not target.get("company_gateway_url") or len(row["encode_key"].encode()) != 16:
                raise ValueError("company login connection missing")
            key = row["operator_trial_key"]
            if len(key) < 32 or key in keys or not row["gateway_internal_token"]:
                raise ValueError("environment exchange credentials must be distinct")
            keys.add(key)
            data = (
                settings.data_dir
                if identifier == settings.dify.environment
                else str(Path(settings.data_dir) / "environments" / identifier)
            )
            data_path = Path(data).absolute()
            if data_path.is_symlink() or any(p.is_symlink() for p in data_path.parents):
                raise ValueError("environment mappings path contains symlinks")
            if (data_path / "mappings.db").is_symlink():
                raise ValueError("environment mappings must be isolated")
            if identifier != settings.dify.environment:
                if Path(data).resolve() in roots:
                    raise ValueError("environment mappings must be isolated")
                roots.add(Path(data).resolve())
            selected = replace(
                settings,
                data_dir=data,
                company=replace(
                    settings.company,
                    base_url=target["company_gateway_url"],
                    encode_key=row["encode_key"],
                    basic_auth=row["basic_auth"],
                    client_type=row["client_type"],
                    info_client_type=row["info_client_type"],
                ),
                dify=replace(settings.dify, environment=identifier),
                gateway_url=target["gateway_url"],
                gateway_internal_token=row["gateway_internal_token"],
                operator_trial_key=key,
            )
            self.entries[identifier] = (selected, build(selected), registry)
        if settings.dify.environment not in self.entries:
            raise ValueError("default login environment must be registered")

    @property
    def enabled(self):
        return bool(self.settings.environment_credentials_file)

    def select(self, identifier, *, allow_disabled=False):
        from aiops_diagnostics.gateway.environment_registry import RegistryError

        entry = self.entries.get(identifier) if isinstance(identifier, str) else None
        if entry is None:
            raise refused("ENVIRONMENT_UNAVAILABLE", "所选环境尚未准备好")
        settings, service, registry = entry
        if registry is not None:
            try:
                row = registry.entry()
                if (not allow_disabled and not row["enabled"]) or row[
                    "company_gateway_url"
                ] != settings.company.base_url:
                    raise RegistryError("ENVIRONMENT_UNAVAILABLE")
            except RegistryError as exc:
                raise refused("ENVIRONMENT_UNAVAILABLE", "所选环境已停用或配置已更新，请重新登录") from exc
        return settings, service

    def catalogue(self):
        from aiops_diagnostics.gateway.environment_registry import RegistryError

        from .core import PermissionDenied

        rows = []
        for identifier, (_, _, registry) in self.entries.items():
            try:
                self.select(identifier)
                rows.append({"id": identifier, "name": registry.entry()["name"] if registry else identifier})
            except (PermissionDenied, RegistryError):
                continue
        return {"environments": rows, "selection_required": self.enabled}

    def challenge(self, random_str, environment, *, consume=False, source=""):
        if not self.enabled:
            return
        key = hashlib.sha256(random_str.encode()).hexdigest()
        with self.lock:
            now = time.monotonic()
            self.challenges = {k: v for k, v in self.challenges.items() if v[1] > now}
            if consume:
                value = self.challenges.pop(key, None)
                if value is None or value[0] != environment:
                    raise refused("CAPTCHA_ENVIRONMENT_MISMATCH", "请为所选环境重新获取验证码")
            else:
                source = hashlib.sha256(source.encode()).hexdigest()
                if sum(v[2] == source for v in self.challenges.values()) >= 30:
                    raise refused("CAPTCHA_RATE_LIMIT", "验证码获取过于频繁，请稍后再试")
                if key in self.challenges or len(self.challenges) >= 4096:
                    raise refused("CAPTCHA_UNAVAILABLE", "请重新获取验证码")
                self.challenges[key] = environment, now + 300, source

    def discard_challenge(self, random_str):
        with self.lock:
            self.challenges.pop(hashlib.sha256(random_str.encode()).hexdigest(), None)

    def session(self, token):
        from aiops_diagnostics.gateway.environment_registry import RegistryError

        from .core import PermissionDenied

        if not isinstance(token, str) or not token or len(token) > 16384:
            raise refused("SESSION_LOGIN_REQUIRED", "请重新登录")
        found = []
        for identifier, (_, service, _) in self.entries.items():
            try:
                service.active_session(token)
                found.append(identifier)
            except (PermissionDenied, RegistryError):
                continue
        if len(found) != 1:
            raise refused("SESSION_LOGIN_REQUIRED", "请重新登录")
        return self.select(found[0], allow_disabled=True)
