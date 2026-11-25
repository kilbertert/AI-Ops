"""控制台发布桥的多环境私有凭据读取（仅依赖标准库与共享原语）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.gateway.deployment_environment import canonical_environment


def credentials(path: str | Path) -> dict[str, dict[str, Any]]:
    """读取并校验 ``aiops-console-environments/1`` 格式的私有环境凭据文件。"""
    target = Path(path)
    if any(parent.is_symlink() for parent in target.absolute().parents):
        raise ValueError("console credentials path contains symlinks")
    validate_private_file(target)
    if target.stat().st_size > 262144:
        raise ValueError("console credentials too large")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate console credential field")
            result[key] = value
        return result

    body = json.loads(target.read_text(), object_pairs_hook=unique)
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "credentials"}
        or body["schema"] != "aiops-console-environments/1"
    ):
        raise ValueError("invalid console environment credentials")
    rows = body["credentials"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("invalid console credential count")
    result: dict[str, dict[str, Any]] = {}
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
