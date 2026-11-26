"""按 (revision, python_minor) 索引的控制面离线制品池（ADR-0358 P1-3）。

新客户开通时按目标机自动探测到的 Python 小版本（``3.11`` / ``3.12`` / ``3.13``）自动匹配
控制面已验离线应用包与安装器包，彻底消除手工运行 ``build.sh`` 与手改 ``references.json``。
池清单本身以 ``0600`` 私有文件持久化，对外列表接口严禁返回服务端绝对文件路径。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    validate_private_file,
    write_private_text,
)

ARTIFACT_POOL_SCHEMA = "aiops-artifact-pool/1"
SUPPORTED_PYTHON_MINORS = frozenset({"3.11", "3.12", "3.13"})
_HEX_CHARS = frozenset("0123456789abcdef")


class ArtifactPoolRefused(ValueError):
    """离线制品池校验、登记或解析失败时的稳定错误码异常。"""


def _is_sha40(value: str) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(ch in _HEX_CHARS for ch in value)


def _sha256_file(path: Path) -> str:
    validate_private_file(path)
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _load_pool(pool_path: Path) -> dict[str, Any]:
    if not pool_path.exists():
        return {"schema": ARTIFACT_POOL_SCHEMA, "revision": 0, "entries": []}
    validate_private_file(pool_path)
    body = json.loads(pool_path.read_text(encoding="utf-8"))
    if (
        not isinstance(body, dict)
        or body.get("schema") != ARTIFACT_POOL_SCHEMA
        or not isinstance(body.get("entries"), list)
    ):
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_POOL_CORRUPT")
    return body


def register_pool_entry(
    pool_path: Path,
    *,
    revision: str,
    python_minor: str,
    app_archive: Path,
    installer_archive: Path,
    actor: str = "aiops-admin",
) -> dict[str, Any]:
    """核验 0600 离线归档摘要并将 (revision, python_minor) 登记至受管制品池。"""
    if not _is_sha40(revision):
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_REVISION_INVALID")
    if python_minor not in SUPPORTED_PYTHON_MINORS:
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_PYTHON_UNSUPPORTED")
    if not app_archive.is_absolute() or not installer_archive.is_absolute():
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_PATH_INVALID")
    try:
        app_sha = _sha256_file(app_archive)
        inst_sha = _sha256_file(installer_archive)
    except (OSError, ValueError) as exc:
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_ARCHIVE_UNTRUSTED") from exc

    body = _load_pool(pool_path)
    now = time.time()
    new_entry = {
        "revision": revision,
        "python_minor": python_minor,
        "application": {"path": str(app_archive), "sha256": app_sha},
        "installer": {"path": str(installer_archive), "sha256": inst_sha},
        "registered_at": now,
        "registered_by": str(actor),
    }
    entries = [
        e
        for e in body["entries"]
        if not (e.get("revision") == revision and e.get("python_minor") == python_minor)
    ]
    entries.append(new_entry)
    body["entries"] = entries
    body["revision"] = int(body.get("revision") or 0) + 1
    ensure_private_directory(pool_path.parent)
    write_private_text(pool_path, json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {
        "revision": revision,
        "python_minor": python_minor,
        "application_sha256": app_sha,
        "installer_sha256": inst_sha,
        "registered_at": now,
    }


def resolve_artifact_for_python(
    pool_path: Path,
    python_minor: str,
    *,
    preferred_revision: str | None = None,
) -> dict[str, Any]:
    """按目标机 Python 小版本解析匹配的最新（或指定）离线制品，并实时复核磁盘 SHA-256。"""
    if python_minor not in SUPPORTED_PYTHON_MINORS:
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_PYTHON_UNSUPPORTED")
    if preferred_revision is not None and not _is_sha40(preferred_revision):
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_REVISION_INVALID")

    body = _load_pool(pool_path)
    candidates = [
        e
        for e in body["entries"]
        if e.get("python_minor") == python_minor
        and (preferred_revision is None or e.get("revision") == preferred_revision)
    ]
    if not candidates:
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_NOT_FOUND")
    selected = max(candidates, key=lambda item: float(item.get("registered_at") or 0.0))

    app_path = Path(str(selected["application"]["path"]))
    inst_path = Path(str(selected["installer"]["path"]))
    try:
        actual_app_sha = _sha256_file(app_path)
        actual_inst_sha = _sha256_file(inst_path)
    except (OSError, ValueError) as exc:
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_ARCHIVE_UNTRUSTED") from exc

    if (
        actual_app_sha != selected["application"]["sha256"]
        or actual_inst_sha != selected["installer"]["sha256"]
    ):
        raise ArtifactPoolRefused("ENVIRONMENT_ARTIFACT_DIGEST_DRIFT")

    return {
        "revision": str(selected["revision"]),
        "python_minor": python_minor,
        "application": {"path": str(app_path), "sha256": actual_app_sha},
        "installer": {"path": str(inst_path), "sha256": actual_inst_sha},
    }


def list_pool_status(pool_path: Path) -> dict[str, Any]:
    """返回脱敏只读制品池摘要，严禁泄露服务端绝对文件路径。"""
    body = _load_pool(pool_path)
    items = [
        {
            "revision": str(e.get("revision") or ""),
            "python_minor": str(e.get("python_minor") or ""),
            "application_sha256": str((e.get("application") or {}).get("sha256") or ""),
            "installer_sha256": str((e.get("installer") or {}).get("sha256") or ""),
            "registered_at": e.get("registered_at"),
        }
        for e in sorted(
            body["entries"],
            key=lambda x: float(x.get("registered_at") or 0.0),
            reverse=True,
        )
    ]
    return {
        "schema": ARTIFACT_POOL_SCHEMA,
        "pool_revision": int(body.get("revision") or 0),
        "count": len(items),
        "entries": items,
    }
