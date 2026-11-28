"""目录给地址，私有投影给凭据；活动登录选择环境，不向业务端广播会话。"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.gateway.deployment_environment import canonical_environment

_LOGGER = logging.getLogger("aiops.dify_login_proxy.environments")


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
        from aiops_diagnostics.onboarding.registry import EnvironmentRegistry

        self.settings = settings
        self.entries = {}
        self.challenges = {}
        self.lock = threading.Lock()
        self._build = build
        self._explicit_ids: set[str] = set()
        self._control_env = settings.dify.environment
        self._active_registry_file = settings.environment_registry_file
        self._last_sync_at = 0.0
        self._sync_interval = 15.0
        if not settings.environment_credentials_file:
            self.entries[settings.dify.environment] = (settings, default_service or build(settings), None)
            return
        if not settings.environment_registry_file:
            raise ValueError("multi-environment login requires a registry")
        roots = {Path(settings.data_dir).resolve()}
        keys = set()
        required_keys = {
            "id",
            "encode_key",
            "basic_auth",
            "client_type",
            "info_client_type",
            "gateway_internal_token",
            "operator_trial_key",
        }
        optional_keys = {"allow_system_platform_account"}
        for identifier, row in credentials(settings.environment_credentials_file).items():
            if not required_keys.issubset(row) or not set(row).issubset(required_keys | optional_keys):
                raise ValueError("invalid login credential fields")
            if not all(isinstance(row[k], str) for k in required_keys):
                raise ValueError("invalid login credential values")
            if (
                "allow_system_platform_account" in row
                and type(row["allow_system_platform_account"]) is not bool
            ):
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
            self._validate_data_dir(data, identifier=identifier, roots=roots)
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
                workspace_environment=identifier,
                allow_system_platform_account=bool(row.get("allow_system_platform_account", False)),
            )
            self.entries[identifier] = (selected, build(selected), registry)
            self._explicit_ids.add(identifier)
        if settings.dify.environment not in self.entries:
            raise ValueError("default login environment must be registered")
        if settings.auto_registry_environments:
            synced = self._synced_registry_path()
            if synced.is_file():
                try:
                    from aiops_diagnostics.onboarding import registry as env_registry

                    env_registry.load(synced)
                    self._active_registry_file = str(synced)
                except Exception:  # noqa: BLE001 - 本地缓存损坏时平稳回退种子目录
                    self._active_registry_file = settings.environment_registry_file
            try:
                self._materialize_dynamic_entries(self._active_registry_file)
            except Exception as exc:  # noqa: BLE001 - 启动阶段不因种子目录缺少动态项失败
                _LOGGER.warning("dify-login: initial dynamic env load skipped: %s", exc)
            self._maybe_sync_registry(force=True)

    def _synced_registry_path(self) -> Path:
        return Path(self.settings.data_dir) / "synced-registry" / "registry.json"

    def _validate_data_dir(self, data: str, *, identifier: str, roots: set[Path] | None = None) -> None:
        data_path = Path(data).absolute()
        if data_path.is_symlink() or any(p.is_symlink() for p in data_path.parents):
            raise ValueError("environment mappings path contains symlinks")
        if (data_path / "mappings.db").is_symlink():
            raise ValueError("environment mappings must be isolated")
        if roots is not None and identifier != self.settings.dify.environment:
            resolved = Path(data).resolve()
            if resolved in roots:
                raise ValueError("environment mappings must be isolated")
            roots.add(resolved)

    def _maybe_sync_registry(self, *, force: bool = False) -> None:
        if not self.enabled or not self.settings.auto_registry_environments:
            return
        with self.lock:
            now = time.monotonic()
            if not force and (now - self._last_sync_at) < self._sync_interval:
                return
            self._last_sync_at = now
            control_entry = self.entries.get(self._control_env)
            if control_entry is None:
                return
            control_settings = control_entry[0]
        if not control_settings.gateway_url or not control_settings.gateway_internal_token:
            return
        from aiops_diagnostics.dify.dify_workspace_directory import GatewayWorkspaceClient
        from aiops_diagnostics.onboarding import registry as env_registry

        client = GatewayWorkspaceClient(
            base_url=control_settings.gateway_url,
            secret=control_settings.gateway_internal_token,
            timeout=3.0,
        )
        doc = client.fetch_registry()
        if not isinstance(doc, dict) or doc.get("schema") != env_registry.MANAGED_SCHEMA:
            return
        try:
            env_registry.validate(doc.get("environments"), managed=True)
            env_registry.validate_managers(doc.get("managers"), doc.get("environments"))
            content = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
            synced = self._synced_registry_path()
            ensure_private_directory(synced.parent)
            if not synced.is_file() or synced.read_text(encoding="utf-8") != content:
                write_private_text(synced, content)
            env_registry.load(synced)
        except Exception as exc:  # noqa: BLE001 - 控制面瞬时不可用或格式异常时保留既有目录
            _LOGGER.warning("dify-login: registry sync skipped: %s", exc)
            return
        with self.lock:
            self._active_registry_file = str(synced)
            try:
                self._materialize_dynamic_entries(str(synced))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("dify-login: dynamic env refresh skipped: %s", exc)

    def _materialize_dynamic_entries(self, reg_path: str) -> None:
        from aiops_diagnostics.onboarding import registry as env_registry

        body = env_registry.load(reg_path)
        control_entry = self.entries.get(self._control_env)
        if control_entry is None:
            return
        control_settings = control_entry[0]
        env_rows = {str(r["id"]): r for r in body["environments"] if isinstance(r, dict) and r.get("id")}

        # 同步刷新既有显式条目的 registry 指向（保留 service 实例与内存会话缓存）。
        for explicit_id in list(self._explicit_ids):
            if explicit_id not in env_rows or explicit_id not in self.entries:
                continue
            cur_settings, cur_service, _ = self.entries[explicit_id]
            reg = env_registry.EnvironmentRegistry(reg_path, explicit_id)
            cur_service.environment_registry = reg
            self.entries[explicit_id] = (cur_settings, cur_service, reg)

        for env_id, row in env_rows.items():
            if env_id in self._explicit_ids:
                continue
            company_url = str(row.get("company_gateway_url") or "").strip()
            if not company_url:
                continue
            existing = self.entries.get(env_id)
            if existing is not None:
                cur_settings, cur_service, _ = existing
                if cur_settings.company.base_url == company_url:
                    reg = env_registry.EnvironmentRegistry(reg_path, env_id)
                    cur_service.environment_registry = reg
                    self.entries[env_id] = (cur_settings, cur_service, reg)
                    continue
            data = str(Path(self.settings.data_dir) / "environments" / env_id)
            self._validate_data_dir(data, identifier=env_id)
            selected = replace(
                control_settings,
                data_dir=data,
                company=replace(control_settings.company, base_url=company_url),
                dify=replace(control_settings.dify, environment=env_id),
                gateway_url=control_settings.gateway_url,
                gateway_internal_token=control_settings.gateway_internal_token,
                operator_trial_key=control_settings.operator_trial_key,
                environment_registry_file=reg_path,
                workspace_environment=env_id,
                allow_system_platform_account=True,
            )
            reg = env_registry.EnvironmentRegistry(reg_path, env_id)
            service = self._build(selected)
            service.environment_registry = reg
            self.entries[env_id] = (selected, service, reg)

    @property
    def enabled(self):
        return bool(self.settings.environment_credentials_file)

    def select(self, identifier, *, allow_disabled=False):
        from aiops_diagnostics.onboarding.registry import RegistryError

        if self.settings.auto_registry_environments and isinstance(identifier, str):
            self._maybe_sync_registry(force=identifier not in self.entries)
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
        from aiops_diagnostics.onboarding.registry import RegistryError

        from .core import PermissionDenied

        if self.settings.auto_registry_environments:
            self._maybe_sync_registry()
        rows = []
        for identifier, (_, _, registry) in list(self.entries.items()):
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
        from aiops_diagnostics.onboarding.registry import RegistryError

        from .core import PermissionDenied

        if not isinstance(token, str) or not token or len(token) > 16384:
            raise refused("SESSION_LOGIN_REQUIRED", "请重新登录")
        found = []
        for identifier, (_, service, _) in list(self.entries.items()):
            try:
                service.active_session(token)
                found.append(identifier)
            except (PermissionDenied, RegistryError):
                continue
        if len(found) != 1:
            raise refused("SESSION_LOGIN_REQUIRED", "请重新登录")
        return self.select(found[0], allow_disabled=True)
