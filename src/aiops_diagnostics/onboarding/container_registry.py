"""客户机 Gateway OCI 容器镜像注册表抽象、摘要校验与安全运行契约（ADR-0363）。

设计约束：
- 严格要求不可变摘要引用 `<registry>/<repo>@sha256:<64hex>`，禁止 `:latest` 或仅 tag 引用；
- 阿里云 ACR 拉取凭据保存在控制面 `0600` 私有文件 `registry-config.json` 与客户机隔离目录
  `/etc/aiops-access-<env>/docker-config/config.json`，严禁覆盖客户 `/root/.docker/config.json`；
- 容器必须以 `--network host`、`--read-only`、项目专属非 root `--user <uid>:<gid>` 运行，
  使既有 `production.env` 与宿主 `127.0.0.1` Redis/`tsdata`/Nginx 保持零配置改动。
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import validate_private_file

REGISTRY_CONFIG_SCHEMA = "aiops-container-registry/1"
IMAGE_METADATA_SCHEMA = "aiops-gateway-image/1"

_IMAGE_REF_RE = re.compile(
    r"^(?P<registry>[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?)/"
    r"(?P<repo>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?$")
_REPO_PATH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
_ENV_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

_GITHUB_IMAGE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_GITHUB_CACHE_TTL_SEC = 180.0


class ContainerRegistryRefused(ValueError):
    """OCI 镜像摘要、注册表配置或容器运行时契约不合法的稳定错误码异常。"""


def validate_image_ref(image_ref: str) -> tuple[str, str, str]:
    """严格校验不可变 OCI 镜像引用 `<registry>/<repo>@sha256:<64hex>`。"""
    if not isinstance(image_ref, str):
        raise ContainerRegistryRefused("ENVIRONMENT_IMAGE_REF_INVALID")
    candidate = image_ref.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        raise ContainerRegistryRefused("ENVIRONMENT_IMAGE_REF_INVALID")
    matched = _IMAGE_REF_RE.fullmatch(candidate)
    if matched is None:
        raise ContainerRegistryRefused("ENVIRONMENT_IMAGE_REF_INVALID")
    return matched.group("registry"), matched.group("repo"), matched.group("digest")


def is_valid_image_ref(image_ref: Any) -> bool:
    try:
        validate_image_ref(str(image_ref))
    except ContainerRegistryRefused:
        return False
    return True


def is_valid_digest(digest: Any) -> bool:
    return isinstance(digest, str) and bool(_DIGEST_RE.fullmatch(digest.strip()))


def parse_registry_config_bytes(raw: bytes) -> dict[str, Any] | None:
    """解析并校验 `registry-config.json`（含空则退回 None）；不外泄密码字段给日志。"""
    if not raw.strip():
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContainerRegistryRefused("ENVIRONMENT_REGISTRY_CONFIG_INVALID") from exc
    if not isinstance(body, dict) or body.get("schema") != REGISTRY_CONFIG_SCHEMA:
        raise ContainerRegistryRefused("ENVIRONMENT_REGISTRY_CONFIG_INVALID")
    repository = str(body.get("repository") or "").strip()
    registry = str(body.get("registry") or "").strip()
    if not registry and "/" in repository:
        registry = repository.split("/", 1)[0]
    if not _REGISTRY_HOST_RE.fullmatch(registry) or not _REPO_PATH_RE.fullmatch(repository):
        raise ContainerRegistryRefused("ENVIRONMENT_REGISTRY_CONFIG_INVALID")
    return {
        "schema": REGISTRY_CONFIG_SCHEMA,
        "registry": registry,
        "repository": repository,
        "pull_username": str(body.get("pull_username") or "").strip(),
        "pull_password": str(body.get("pull_password") or ""),
        "updated_at": str(body.get("updated_at") or ""),
    }


def load_registry_config(path: Path) -> dict[str, Any] | None:
    """从控制面私有目录读取并验证 `registry-config.json` 权限及内容。"""
    if not path.is_file():
        return None
    validate_private_file(path)
    return parse_registry_config_bytes(path.read_bytes())


def render_customer_docker_config_json(cfg: dict[str, Any] | None) -> str | None:
    """当配置包含独立拉取凭据时生成隔离 `DOCKER_CONFIG/config.json`，否则返回 None。"""
    if not isinstance(cfg, dict):
        return None
    registry = str(cfg.get("registry") or "").strip()
    user = str(cfg.get("pull_username") or "").strip()
    password = str(cfg.get("pull_password") or "")
    if not registry or not user or not password:
        return None
    if not _REGISTRY_HOST_RE.fullmatch(registry) or "\n" in user or "\n" in password:
        raise ContainerRegistryRefused("ENVIRONMENT_REGISTRY_CONFIG_INVALID")
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    doc = {"auths": {registry: {"auth": token}}}
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def render_container_env(env: str, image_ref: str, local_port: int, uid: int, gid: int) -> str:
    """生成 `/opt/aiops-access-<env>/container.env`（零密钥、锁死不可变镜像摘要）。"""
    env_clean = env.strip().lower()
    if not _ENV_ID_RE.fullmatch(env_clean):
        raise ContainerRegistryRefused("ENVIRONMENT_WIZARD_INVALID")
    validate_image_ref(image_ref)
    if not isinstance(local_port, int) or local_port <= 0 or local_port > 65535:
        raise ContainerRegistryRefused("ENVIRONMENT_PORT_INVALID")
    if uid <= 0 or gid <= 0:
        raise ContainerRegistryRefused("ENVIRONMENT_UID_INVALID")
    return "\n".join(
        [
            f"ENV_NAME={env_clean}",
            f"IMAGE_REF={image_ref.strip()}",
            f"LOCAL_PORT={local_port}",
            f"RUN_UID={uid}",
            f"RUN_GID={gid}",
            "",
        ]
    )


def render_container_systemd_unit(env: str) -> str:
    """生成直跑 Docker 容器的平铺 systemd unit（EnvironmentFile 注入，无需中间壳脚本）。"""
    env_clean = env.strip().lower()
    if not _ENV_ID_RE.fullmatch(env_clean):
        raise ContainerRegistryRefused("ENVIRONMENT_WIZARD_INVALID")
    ctr = f"aiops-access-{env_clean}"
    etc_dir = f"/etc/aiops-access-{env_clean}"
    var_dir = f"/var/lib/aiops-access-{env_clean}"
    opt_dir = f"/opt/aiops-access-{env_clean}"
    docker_cmd = " ".join(
        [
            "/usr/bin/docker run --rm",
            f"--name {ctr}",
            "--network host",
            "--user ${RUN_UID}:${RUN_GID}",
            "--read-only",
            "--tmpfs /tmp:rw,noexec,nosuid,size=64m",
            "--security-opt no-new-privileges:true",
            "--cap-drop ALL",
            "--memory 384m --cpus 0.5 --pids-limit 64",
            f"-v {etc_dir}:{etc_dir}:ro",
            f"-v {var_dir}:{var_dir}:rw",
            "-e LANG=C.UTF-8",
            "-e PYTHONNOUSERSITE=1",
            f"-e AIOPS_HOME={var_dir}",
            f"-e AIOPS_DATA_HOME={var_dir}",
            f"-e AIOPS_CONFIG_HOME={var_dir}/config",
            f"-e AIOPS_CODEX_KEY_DIR={var_dir}/keys",
            f"-e AIOPS_AGENT_RUN_ROOT={var_dir}/runs",
            f"-e AIOPS_GATEWAY_DATABASE_FILE={var_dir}/gateway/gateway.db",
            f"-e AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE={var_dir}/gateway/gateway.db",
            f"-e AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR={var_dir}/keys",
            f"-e AIOPS_GATEWAY_SERVER_CONFIG_FILE={etc_dir}/production.env",
            f"-e AIOPS_GATEWAY_ENVIRONMENT={env_clean}",
            "${IMAGE_REF}",
            "serve --host 127.0.0.1 --port ${LOCAL_PORT}",
        ]
    )
    return "\n".join(
        [
            "[Unit]",
            f"Description=AI-Ops managed access {env_clean} (OCI container)",
            "After=network-online.target docker.service",
            "Wants=network-online.target",
            "Requires=docker.service",
            "",
            "[Service]",
            f"EnvironmentFile={opt_dir}/container.env",
            f"ExecStartPre=-/usr/bin/docker rm -f {ctr}",
            f"ExecStart={docker_cmd}",
            f"ExecStop=/usr/bin/docker stop -t 15 {ctr}",
            "Restart=on-failure",
            "RestartSec=3",
            "TimeoutStopSec=25",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def sanitize_image_descriptor(raw: Any) -> dict[str, Any] | None:
    """校验并提取单条版本 OCI 镜像描述符，只保留非敏感字段。"""
    if not isinstance(raw, dict):
        return None
    img_ref = str(raw.get("image_ref") or "").strip()
    if not is_valid_image_ref(img_ref):
        return None
    reg, repo, digest = validate_image_ref(img_ref)
    rev = str(raw.get("revision") or "").strip().lower()
    if rev and not _HEX40_RE.fullmatch(rev):
        return None
    tag = str(raw.get("tag") or "").strip()
    if tag and not _RELEASE_TAG_RE.fullmatch(tag):
        return None
    return {
        "schema": IMAGE_METADATA_SCHEMA,
        "tag": tag,
        "revision": rev,
        "image_ref": img_ref,
        "image_digest": digest,
        "image_repo": f"{reg}/{repo}",
        "image_tag": str(raw.get("image_tag") or tag),
        "ingress_sha256": str(raw.get("ingress_sha256") or ""),
    }


def _resolve_github_repo_slug(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    remote = proc.stdout.strip()
    matched = re.search(r"github\.com[:/](?P<slug>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?$", remote)
    return matched.group("slug") if matched else ""


def discover_release_images_from_github(
    repo_root: Path,
    releases: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """从 GitHub Releases 附件 `gateway-image.json` 发现正式版本的不可变镜像摘要（带缓存、失败静默降级）。"""
    slug = _resolve_github_repo_slug(repo_root)
    if not slug:
        return {}

    now = time.time()
    discovered: dict[str, dict[str, Any]] = {}
    for item in releases:
        tag = str(item.get("tag") or "").strip()
        rev = str(item.get("revision") or "").strip().lower()
        if not tag or not _HEX40_RE.fullmatch(rev):
            continue
        cache_key = f"{slug}:{tag}:{rev}"
        cached = _GITHUB_IMAGE_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _GITHUB_CACHE_TTL_SEC:
            if cached[1] is not None:
                discovered[rev] = cached[1]
            continue

        desc: dict[str, Any] | None = None
        try:
            rel_proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{slug}/releases/tags/{tag}",
                    "--jq",
                    '.assets[] | select(.name == "gateway-image.json") | .url',
                ],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=15,
            )
            asset_url = (
                rel_proc.stdout.strip().splitlines()[0]
                if rel_proc.returncode == 0 and rel_proc.stdout.strip()
                else ""
            )
            if asset_url:
                dl_proc = subprocess.run(
                    ["gh", "api", asset_url, "-H", "Accept: application/octet-stream"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if dl_proc.returncode == 0 and dl_proc.stdout.strip():
                    raw_meta = json.loads(dl_proc.stdout)
                    parsed = sanitize_image_descriptor(raw_meta)
                    if parsed and (not parsed.get("revision") or parsed["revision"] == rev):
                        parsed["revision"] = rev
                        parsed["tag"] = tag
                        desc = parsed
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            desc = None

        _GITHUB_IMAGE_CACHE[cache_key] = (now, desc)
        if desc is not None:
            discovered[rev] = desc
    return discovered
