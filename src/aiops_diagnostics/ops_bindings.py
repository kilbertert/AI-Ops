"""运维服务凭据只授予登记目标；每次读取重新验证私有文件。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops_diagnostics.private_files import PrivatePathError, validate_private_file


class OpsBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    environment: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    app_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    active: bool


def load_ops_bindings(filename: str) -> tuple[OpsBinding, ...]:
    if not filename:
        return ()
    try:
        path = Path(filename)
        validate_private_file(path)
        with path.open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > 1000:
            raise ValueError
        bindings = tuple(OpsBinding.model_validate(row) for row in data)
        if len({row.key_sha256 for row in bindings}) != len(bindings):
            raise ValueError
        return bindings
    except (OSError, ValueError, ValidationError, PrivatePathError) as exc:
        raise ValueError("运维工具绑定文件不可用或格式无效") from exc


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ops_app_registered(filename: str, environment: str, tenant_id: str, app_id: str) -> bool:
    # 即使凭据停用，发布仍须理解这一执行面的空名单语义。
    return any(
        row.environment == environment and row.tenant_id == tenant_id and row.app_id == app_id
        for row in load_ops_bindings(filename)
    )
