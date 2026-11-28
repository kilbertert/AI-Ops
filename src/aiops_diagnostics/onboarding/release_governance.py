"""Day-2 已上线客户环境版本目录、无损升级与回滚状态账本（ADR-0361）。

约束：
- 官方可选目标版本严格限定为 `origin/main` 祖先上的不可变 SemVer 注释标签（`vX.Y.Z`）；
- 所有状态文件均落盘为 `0600` 私有 JSON，严禁存储或回传服务端绝对文件路径或任何凭据；
- 返回给管理面的差异对比明确包含目标版本相对客户机现役 Nginx 入口的路由变更清单。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.onboarding import (
    artifact_pool,
    container_registry,
    host_enrollment,
    registry,
    wizard_jobs,
)

RELEASE_CATALOG_SCHEMA = "aiops-release-catalog/1"
ENV_RELEASE_STATE_SCHEMA = "aiops-environment-releases/1"

_RELEASE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_ENV_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseGovernanceRefused(ValueError):
    """Day-2 版本校验、目录读取或升级/回滚前置校验失败的稳定错误码异常。"""


def is_valid_release_tag(tag: Any) -> bool:
    return isinstance(tag, str) and bool(_RELEASE_TAG_RE.fullmatch(tag.strip()))


def assets_dir_for_registry(registry_path: str | Path) -> Path:
    """定位控制面受管运行资产目录，兼容独立单测隔离临时目录。"""
    resolved = Path(registry_path).resolve()
    onboarding_assets = resolved.parent.parent / "onboarding-assets"
    if onboarding_assets.is_dir():
        return onboarding_assets
    # 生产固定布局 `/etc/aiops-41/registry/registry.json` -> `/var/lib/aiops-41/onboarding-assets`
    prod_assets = Path("/var/lib/aiops-41/onboarding-assets")
    if resolved == Path("/etc/aiops-41/registry/registry.json") and prod_assets.is_dir():
        return prod_assets
    if (resolved.parent / "release-catalog.json").is_file() or (
        resolved.parent / "artifact-pool.json"
    ).is_file():
        return resolved.parent
    return onboarding_assets if onboarding_assets.parent.name == "aiops-41" else resolved.parent


def _catalog_path(assets_dir: Path) -> Path:
    return assets_dir / "release-catalog.json"


def _env_states_path(assets_dir: Path) -> Path:
    return assets_dir / "environment-releases.json"


def _pool_path(registry_path: str | Path, assets_dir: Path) -> Path:
    candidate = assets_dir / "artifact-pool.json"
    if candidate.is_file():
        return candidate
    return Path(registry_path).resolve().parent / "artifact-pool.json"


def load_release_catalog(assets_dir: Path) -> dict[str, Any]:
    path = _catalog_path(assets_dir)
    if not path.is_file():
        return {"schema": RELEASE_CATALOG_SCHEMA, "updated_at": None, "entries": []}
    validate_private_file(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("entries"), list):
        return {"schema": RELEASE_CATALOG_SCHEMA, "updated_at": None, "entries": []}
    return {
        "schema": RELEASE_CATALOG_SCHEMA,
        "updated_at": body.get("updated_at"),
        "entries": list(wizard_jobs.sanitize_public_dict(body["entries"])),
    }


def save_release_catalog(assets_dir: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise ReleaseGovernanceRefused("ENVIRONMENT_RELEASE_CATALOG_INVALID")
    sanitized: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ReleaseGovernanceRefused("ENVIRONMENT_RELEASE_CATALOG_INVALID")
        tag = str(raw.get("tag") or "").strip()
        rev = str(raw.get("revision") or "").strip().lower()
        if not is_valid_release_tag(tag) or not _HEX40_RE.fullmatch(rev):
            raise ReleaseGovernanceRefused("ENVIRONMENT_RELEASE_CATALOG_INVALID")
        clean = dict(wizard_jobs.sanitize_public_dict(raw))
        clean["tag"] = tag
        clean["version"] = str(clean.get("version") or tag[1:])
        clean["revision"] = rev
        clean["short_sha"] = rev[:12]
        clean["full_version"] = str(clean.get("full_version") or f"{clean['version']}+{rev[:12]}")
        routes = [str(r).strip() for r in (clean.get("ingress_routes") or []) if str(r).strip()]
        clean["ingress_routes"] = routes
        clean["ingress_sha256"] = str(clean.get("ingress_sha256") or "")
        clean["pool_python_minors"] = sorted(
            {
                str(m).strip()
                for m in (clean.get("pool_python_minors") or [])
                if str(m).strip() in artifact_pool.SUPPORTED_PYTHON_MINORS
            }
        )
        raw_img_ref = str(clean.get("image_ref") or "").strip()
        if raw_img_ref and container_registry.is_valid_image_ref(raw_img_ref):
            reg_host, reg_repo, img_digest = container_registry.validate_image_ref(raw_img_ref)
            clean["image_ref"] = raw_img_ref
            clean["image_digest"] = img_digest
            clean["image_repo"] = f"{reg_host}/{reg_repo}"
            clean["image_tag"] = str(clean.get("image_tag") or tag)
            clean["image_ready"] = True
            clean["distribution_mode"] = "oci"
        else:
            clean["image_ref"] = ""
            clean["image_digest"] = ""
            clean["image_repo"] = ""
            clean["image_tag"] = ""
            clean["image_ready"] = False
            clean["distribution_mode"] = "legacy_tarball"
        sanitized.append(clean)

    ensure_private_directory(assets_dir)
    payload = {
        "schema": RELEASE_CATALOG_SCHEMA,
        "updated_at": time.time(),
        "entries": sanitized,
    }
    write_private_text(_catalog_path(assets_dir), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "count": len(sanitized)}


def load_environment_release_states(assets_dir: Path) -> dict[str, Any]:
    path = _env_states_path(assets_dir)
    if not path.is_file():
        return {"schema": ENV_RELEASE_STATE_SCHEMA, "environments": {}}
    validate_private_file(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    envs = body.get("environments") if isinstance(body, dict) else None
    if not isinstance(envs, dict):
        return {"schema": ENV_RELEASE_STATE_SCHEMA, "environments": {}}
    return {
        "schema": ENV_RELEASE_STATE_SCHEMA,
        "environments": dict(wizard_jobs.sanitize_public_dict(envs)),
    }


def save_environment_release_state(
    assets_dir: Path, environment: str, state: dict[str, Any]
) -> dict[str, Any]:
    env = environment.strip().lower()
    if not _ENV_ID_RE.fullmatch(env) or not isinstance(state, dict):
        raise ReleaseGovernanceRefused("ENVIRONMENT_WIZARD_INVALID")
    ensure_private_directory(assets_dir)
    doc = load_environment_release_states(assets_dir)
    clean_state = dict(wizard_jobs.sanitize_public_dict(state))
    # 严禁保留服务端绝对路径（例如 release_dir 仅内部使用，展示层只暴露 safe 字段或脱敏版本名）。
    rollback = clean_state.get("rollback_target")
    if isinstance(rollback, dict):
        safe_rb = {
            "version": str(rollback.get("version") or ""),
            "revision": str(rollback.get("revision") or ""),
            "release_name": str(rollback.get("release_name") or ""),
            "runtime_mode": str(rollback.get("runtime_mode") or ""),
            "image_digest": str(rollback.get("image_digest") or ""),
            "recorded_at": rollback.get("recorded_at"),
            "has_nginx_backup": bool(rollback.get("has_nginx_backup")),
        }
        clean_state["rollback_target"] = safe_rb if safe_rb["version"] else None
    clean_state["environment"] = env
    clean_state["updated_at"] = time.time()
    doc["environments"][env] = clean_state
    write_private_text(_env_states_path(assets_dir), json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "environment": env}


def parse_ingress_routes_text(raw_text: str) -> list[str]:
    routes: list[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        routes.append(stripped)
    return routes


def render_ingress_sha256(user_conf: str, proxy_inc: str) -> str:
    combined = user_conf.rstrip() + "\n---\n" + proxy_inc.rstrip() + "\n"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def compute_ingress_diff(
    current_routes: list[str] | None,
    target_routes: list[str] | None,
    current_sha: str = "",
    target_sha: str = "",
) -> dict[str, Any]:
    cur_list = [str(r).strip() for r in (current_routes or []) if str(r).strip()]
    tgt_list = [str(r).strip() for r in (target_routes or []) if str(r).strip()]
    cur_set = set(cur_list)
    tgt_set = set(tgt_list)
    added = [r for r in tgt_list if r not in cur_set]
    removed = [r for r in cur_list if r not in tgt_set]
    if current_sha and target_sha:
        changed = current_sha != target_sha or bool(added or removed)
    else:
        changed = bool(added or removed)
    return {
        "changed": changed,
        "added_routes": added,
        "removed_routes": removed,
    }


def _git_output(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15,
    )


def collect_git_annotated_releases(
    repo_root: Path,
    *,
    pool_entries: list[dict[str, Any]] | None = None,
    image_entries: dict[str, dict[str, Any]] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """从仓库收集 origin/main 祖先上的官方不可变注释 SemVer 标签。"""
    tag_proc = _git_output(repo_root, "tag", "-l", "v*", "--sort=-v:refname")
    if tag_proc.returncode != 0:
        return []

    pool_by_rev: dict[str, set[str]] = {}
    for entry in pool_entries or []:
        if not isinstance(entry, dict):
            continue
        rev_key = str(entry.get("revision") or "").strip().lower()
        minor = str(entry.get("python_minor") or "").strip()
        if rev_key and minor in artifact_pool.SUPPORTED_PYTHON_MINORS:
            pool_by_rev.setdefault(rev_key, set()).add(minor)

    releases: list[dict[str, Any]] = []
    for raw_tag in tag_proc.stdout.splitlines():
        tag = raw_tag.strip()
        if not is_valid_release_tag(tag):
            continue
        obj_type = _git_output(repo_root, "cat-file", "-t", tag)
        if obj_type.returncode != 0 or obj_type.stdout.strip() != "tag":
            continue
        rev_proc = _git_output(repo_root, "rev-parse", "--verify", f"{tag}^{{commit}}")
        if rev_proc.returncode != 0:
            continue
        revision = rev_proc.stdout.strip().lower()
        if not _HEX40_RE.fullmatch(revision):
            continue
        anc_proc = _git_output(repo_root, "merge-base", "--is-ancestor", revision, "origin/main")
        if anc_proc.returncode != 0:
            continue

        meta_proc = _git_output(repo_root, "log", "-1", "--format=%cI%n%s", revision)
        released_at = ""
        summary = ""
        if meta_proc.returncode == 0:
            lines = meta_proc.stdout.splitlines()
            released_at = lines[0].strip() if len(lines) >= 1 else ""
            summary = lines[1].strip() if len(lines) >= 2 else ""

        routes_proc = _git_output(
            repo_root, "show", f"{revision}:ops/nginx/environment-user-ingress-routes.txt"
        )
        ingress_routes = parse_ingress_routes_text(routes_proc.stdout) if routes_proc.returncode == 0 else []
        conf_proc = _git_output(repo_root, "show", f"{revision}:ops/nginx/environment-user-ingress.conf")
        inc_proc = _git_output(repo_root, "show", f"{revision}:ops/nginx/environment-user-ingress-proxy.inc")
        ingress_sha256 = (
            render_ingress_sha256(conf_proc.stdout, inc_proc.stdout)
            if conf_proc.returncode == 0 and inc_proc.returncode == 0
            else ""
        )

        version = tag[1:]
        img_meta = (image_entries or {}).get(revision) if isinstance(image_entries, dict) else None
        clean_img = container_registry.sanitize_image_descriptor(img_meta) if img_meta else None
        releases.append(
            {
                "tag": tag,
                "version": version,
                "revision": revision,
                "short_sha": revision[:12],
                "full_version": f"{version}+{revision[:12]}",
                "released_at": released_at,
                "summary": summary,
                "ingress_routes": ingress_routes,
                "ingress_sha256": ingress_sha256,
                "pool_python_minors": sorted(pool_by_rev.get(revision, set())),
                "image_ref": clean_img["image_ref"] if clean_img else "",
                "image_digest": clean_img["image_digest"] if clean_img else "",
                "image_repo": clean_img["image_repo"] if clean_img else "",
                "image_tag": clean_img["image_tag"] if clean_img else "",
                "image_ready": bool(clean_img),
                "distribution_mode": "oci" if clean_img else "legacy_tarball",
            }
        )
        if len(releases) >= limit:
            break
    return releases


def _resolve_registry_env(registry_path: str | Path, environment: str) -> dict[str, Any] | None:
    try:
        body = registry.load(registry_path)
    except Exception:
        return None
    for bucket in body.get("environments") or []:
        if isinstance(bucket, dict) and str(bucket.get("id") or "") == environment:
            return bucket
    return None


def build_release_overview(
    registry_path: str | Path,
    environment: str,
    *,
    identity: tuple[str, str, str] | None = None,
    control_version: str = "",
    assets_dir: Path | None = None,
) -> dict[str, Any]:
    """构建 Dify「版本与发版」页所需的只读版本视图、入口路由差异与历史。"""
    env = str(environment or "").strip().lower()
    if not _ENV_ID_RE.fullmatch(env):
        raise ReleaseGovernanceRefused("ENVIRONMENT_WIZARD_INVALID")

    resolved_assets = assets_dir or assets_dir_for_registry(registry_path)
    catalog_doc = load_release_catalog(resolved_assets)
    states_doc = load_environment_release_states(resolved_assets)
    env_state = dict(states_doc.get("environments", {}).get(env) or {})

    pool_summary = {"count": 0, "entries": []}
    pool_file = _pool_path(registry_path, resolved_assets)
    if pool_file.is_file():
        try:
            pool_summary = artifact_pool.list_pool_status(pool_file)
        except Exception:
            pool_summary = {"count": 0, "entries": []}

    pool_by_rev: dict[str, set[str]] = {}
    for item in pool_summary.get("entries") or []:
        if isinstance(item, dict) and item.get("revision") and item.get("python_minor"):
            pool_by_rev.setdefault(str(item["revision"]), set()).add(str(item["python_minor"]))

    enrolled = False
    if identity is not None:
        host_status = host_enrollment.get_host_status(registry_path, identity, env)
        enrolled = bool(host_status.get("enrolled"))
    else:
        try:
            host_body = host_enrollment._load_hosts_unlocked(registry_path)
            host_entry = (host_body.get("hosts") or {}).get(env)
            enrolled = isinstance(host_entry, dict) and host_entry.get("status") in {"enrolled", "verified"}
        except Exception:
            enrolled = False
    reg_env = _resolve_registry_env(registry_path, env)
    gateway_url = str((reg_env or {}).get("company_gateway_url") or "")

    is_control = env == "41"
    current_version = control_version if is_control else str(env_state.get("current_version") or "")
    current_revision = str(env_state.get("current_revision") or "")
    if not current_revision and "+" in current_version:
        current_revision = current_version.split("+", 1)[1].strip()
    current_routes = list(env_state.get("current_ingress_routes") or [])
    current_ingress_sha = str(env_state.get("current_ingress_sha256") or "")

    catalog_entries: list[dict[str, Any]] = []
    for entry in catalog_doc.get("entries") or []:
        rev = str(entry.get("revision") or "")
        minors = sorted(set(entry.get("pool_python_minors") or []) | pool_by_rev.get(rev, set()))
        diff = compute_ingress_diff(
            current_routes,
            entry.get("ingress_routes") or [],
            current_sha=current_ingress_sha,
            target_sha=str(entry.get("ingress_sha256") or ""),
        )
        # 若当前主机记录的网关版本与目标完整版本相同但缺 ingress sha（老手工升级），仍允许对比路由列表。
        img_ref = str(entry.get("image_ref") or "").strip()
        img_ready = container_registry.is_valid_image_ref(img_ref)
        catalog_entries.append(
            {
                "tag": entry["tag"],
                "version": entry["version"],
                "revision": rev,
                "short_sha": entry.get("short_sha") or rev[:12],
                "full_version": entry.get("full_version") or f"{entry['version']}+{rev[:12]}",
                "released_at": entry.get("released_at") or "",
                "summary": entry.get("summary") or "",
                "ingress_routes": list(entry.get("ingress_routes") or []),
                "ingress_diff": diff,
                "pool_python_minors": minors,
                "image_ref": img_ref if img_ready else "",
                "image_digest": str(entry.get("image_digest") or "") if img_ready else "",
                "image_repo": str(entry.get("image_repo") or "") if img_ready else "",
                "image_tag": str(entry.get("image_tag") or "") if img_ready else "",
                "image_ready": img_ready,
                "distribution_mode": "oci" if img_ready else "legacy_tarball",
            }
        )

    latest = catalog_entries[0] if catalog_entries else None
    update_available = bool(latest and current_version and current_version != latest["full_version"])
    history = wizard_jobs.list_jobs_for_environment(
        registry_path,
        env,
        actions=("release_probe", "environment_upgrade", "environment_rollback"),
        limit=10,
    )

    return {
        "schema": "aiops-release-overview/1",
        "environment": env,
        "is_control": is_control,
        "control_version": control_version,
        "enrolled": enrolled,
        "gateway_url": gateway_url,
        "current_version": current_version,
        "current_revision": current_revision,
        "python_minor": str(env_state.get("python_minor") or ""),
        "runtime_mode": str(
            env_state.get("runtime_mode") or ("venv" if current_version and not is_control else "")
        ),
        "current_image_digest": str(env_state.get("current_image_digest") or ""),
        "current_image_ref": str(env_state.get("current_image_ref") or ""),
        "docker_available": env_state.get("docker_available"),
        "business_mutations": str(
            env_state.get("business_mutations") or ("disabled" if current_version else "")
        ),
        "health_ok": env_state.get("health_ok"),
        "last_probed_at": env_state.get("last_probed_at"),
        "current_ingress_routes": current_routes,
        "rollback_target": env_state.get("rollback_target"),
        "catalog_updated_at": catalog_doc.get("updated_at"),
        "available_releases": catalog_entries,
        "latest_release": latest,
        "update_available": update_available,
        "history": history,
    }
