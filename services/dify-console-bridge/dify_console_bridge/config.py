"""Dify 控制台发布桥的配置、常量、输入解析与响应模型。"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.common.async_job_lifecycle import DIAGNOSIS as DIAGNOSIS_PROFILE

_LOGGER = logging.getLogger("aiops.dify_console_publish")

#: 桥只打这三跳，都是相对路径 —— "打到谁"由部署配置决定。
DIFY_WORKSPACE_SUMMARY_PATH = "/console/api/workspaces/current/summary"
DIFY_APP_PATH = "/console/api/apps/{app_id}"
#: 机器门（共享密钥）。**不是** ``/v1/operator/publish``：那条要运营的网关会话。
PUBLISH_PATH = "/v1/dify/publish"
#: 试运行（#781 / #782 的部署层）。草稿由**网关**自己从 Dify 读回来，桥只递 app_id。
TRIAL_PATH = "/v1/dify/trial"

#: Dify ``TenantAccountRole`` 的全部取值（``api/models/account.py``）。
KNOWN_ROLES = frozenset({"owner", "admin", "editor", "normal", "dataset_operator"})
#: 默认只放行 Dify 自己的 ``is_privileged_role`` 集合。
DEFAULT_ALLOWED_ROLES = frozenset({"owner", "admin"})

#: 网关 ``SyncOutcome.action``（``dify_dsl_pull.py``）：前两种是成功，后两种是拒绝。
_PUBLISHED_ACTIONS = frozenset({"synced", "unchanged"})
_REFUSED_ACTIONS = frozenset({"refused", "failed"})

#: 请求体只允许为空或 ``{}``；上限远小于 nginx 的 ``client_max_body_size 4k``。
MAX_REQUEST_BODY_BYTES = 256
#: 上游响应体上限。超限即截断 → JSON 解码失败 → 按"这一步没做成"处置（失败关闭）。
MAX_UPSTREAM_RESPONSE_BYTES = 1024 * 1024

_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,6}")

_PUBLISH_ROUTE = "/__aiops/publish"
_TRIAL_ROUTE = "/__aiops/trial"
MAX_TRIAL_BODY_BYTES = 32 * 1024
_TRIAL_PAYLOAD_KEYS = frozenset({"order_no", "question"})

_TRIAL_RESULT_ROUTE = "/__aiops/trial/result"
_TRIAL_RESULT_PAYLOAD_KEYS = frozenset({"trial_id"})

#: 试运行终态词表**只有一处**（``async_job_lifecycle.DIAGNOSIS.terminal``）。
TRIAL_TERMINAL_STATUSES: frozenset[str] = DIAGNOSIS_PROFILE.terminal

_PUBLISH_CHECK_PATH = "/__aiops/publish/check"
_KNOWLEDGE_OPEN_ROUTE = "/__aiops/knowledge-open"
_KNOWLEDGE_SYNC_ROUTE = "/__aiops/knowledge-sync"
_CONSOLE_ROUTES = frozenset({"/aiops", "/aiops/", "/aiops/environments", "/aiops/environments/"})
_CONSOLE_ASSET_PREFIX = "/aiops/assets/"
DEFAULT_CONSOLE_DIST_DIR = "/opt/aiops-edge/console-dist"

_REFUSED_PUBLISH_PERMISSION = "公司账号没有发布到 AI-Ops 的权限，请联系公司管理员"
_REFUSED_NO_WORKSPACE = "你所在的租户还没有可用的 Dify 工作区，不能发布。请联系管理员为该租户开户后再试"
_REFUSED_SESSION = "Dify 会话已失效或你看不到这个智能体，请重新登录后再试"
_REFUSED_ROLE = "只有工作空间的所有者或管理员可以发布到线上"
_REFUSED_WORKSPACE = "当前不在可发布的工作空间，请切换到正确的工作空间后再试"
_UNCONFIRMED = "发布结果无法确认，请到 AI-Ops 核对线上版本后再试"

#: 网关拒绝时**能照着做**的那几种。其余一律仍回 ``_UNCONFIRMED``。
_GATEWAY_REFUSALS = {
    "OPERATOR_TRIAL_LOGIN_REQUIRED": "公司登录已过期（控制台约 15 分钟需重新登录一次），请重新登录后再试",
    "OPERATOR_TRIAL_LOGIN_CHANGED": "登录身份已变化，请重新登录后再试",
    "CONSUMER_FLOW_INVALID": "",
    "NODE_TARGET_MISSING": "这个智能体还没配置可用的执行节点，请联系工程",
    "CHAT_ROUTE_BINDING_MISMATCH": "这条路由指向了另一个智能体，请联系工程核对后再发布",
}

_MAX_REASON_CHARS = 200
MAX_TRIAL_RESULT_TEXT = 4000

NATIVE_PREVIEW_ROUTE = "/__aiops/operator-preview"
NATIVE_CANCEL_ROUTE = NATIVE_PREVIEW_ROUTE + "/cancel"
NATIVE_POLL_SECONDS = 1.0
NATIVE_TIMEOUT_SECONDS = 180
NATIVE_TASK_PREFIX = "aiops_"


class _KnowledgeRefused(Exception):
    """网关给出一个运营**能行动**的拒绝（未开户 / 链接过期）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ConsolePublishError(RuntimeError):
    """桥自己的一步失败了（不是"运营无权"，而是"这一步没做成"）。"""


class _WorkspaceLookupFailed(Exception):
    """内部信号：查询失败。"""


class _Rejected(Exception):  # noqa: N818 - 控制流信号，不是错误
    """请求在问任何上游之前就被拒了。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConsolePublishResult:
    """一次按钮点击的结果 —— 面向前端，刻意只有三个字段（``status`` 只给 HTTP 面用）。"""

    published: bool
    reason: str
    action: str = ""
    status: int = 403
    trial_id: str = ""
    versions: tuple[dict[str, Any], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "reason": self.reason,
            "action": self.action,
            **({"versions": list(self.versions)} if self.versions else {}),
        }


@dataclass(frozen=True, slots=True)
class TrialReport:
    """一次结果读取的回答：可读的一句话 + 原始状态。"""

    ok: bool
    reason: str
    status: int = 200
    job_status: str = ""
    terminal: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "read": self.ok,
            "reason": self.reason,
            "status": self.job_status,
            "terminal": self.terminal,
        }


def _escape(text: str) -> str:
    """最小 HTML 转义：这段文案里可能有来自上游的字符串，别让它变成标记。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _str_field(payload: Any, key: str) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _origin_and_path(raw: str | None) -> tuple[str, urllib.parse.SplitResult] | None:
    """把一个绝对 http(s) URL 拆成（规范化的源, 拆分结果）；不是就 ``None``。"""
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in {"http", "https"} or not host or "@" in parts.netloc:
        return None
    if ":" in host:  # IPv6 字面量
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    origin = f"{scheme}://{host}" if port in {None, default_port} else f"{scheme}://{host}:{port}"
    return origin, parts


def normalize_origin(raw: str | None) -> str | None:
    """规范化一个**源**（只有协议/主机/端口，可带一个结尾 ``/``）；否则 ``None``。"""
    split = _origin_and_path(raw)
    if split is None:
        return None
    origin, parts = split
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        return None
    return origin


def _is_canonical_uuid(candidate: str) -> bool:
    """Dify 的 app id 是 uuid4，路由里是小写带连字符的规范形；别的形状一律不认。"""
    try:
        return str(uuid.UUID(candidate)) == candidate
    except ValueError:
        return False


def app_id_from_referer(referer: str | None, *, console_origin: str) -> str | None:
    """从**控制台本源页面**的 URL 里取出当前 app 的 id，取不到就是 ``None``。"""
    expected = normalize_origin(console_origin)
    split = _origin_and_path(referer)
    if expected is None or split is None:
        return None
    origin, parts = split
    if origin != expected:
        return None
    segments = parts.path.split("/")
    if len(segments) < 4 or segments[0] != "" or segments[1] != "app" or not segments[3]:
        return None
    candidate = segments[2]
    return candidate if _is_canonical_uuid(candidate) else None


def parse_allowed_roles(raw: str | None) -> frozenset[str]:
    """解析 ``AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES``（逗号分隔）；缺省为所有者/管理员。"""
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_ROLES
    roles = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not roles:
        raise ValueError("AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES must name at least one role")
    unknown = roles - KNOWN_ROLES
    if unknown:
        raise ValueError(f"AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES has unknown roles: {sorted(unknown)}")
    return roles


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """桥的全部配置。**没有一样进浏览器** —— 这正是这一层的要点。"""

    listen_host: str
    listen_port: int
    dify_base_url: str
    gateway_base_url: str
    publish_key: str
    console_origin: str
    workspace_id: str
    gateway_internal_token: str = field(repr=False, default="")
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES
    trial_key: str = ""
    login_proxy_url: str = ""
    timeout: float = 15.0
    environment_credentials_file: str = ""
    console_dist_dir: str = DEFAULT_CONSOLE_DIST_DIR

    def __post_init__(self) -> None:
        if normalize_origin(self.console_origin) != self.console_origin:
            raise ValueError("console_origin must be a normalized origin (scheme://host[:port])")
        if not self.workspace_id or self.workspace_id != self.workspace_id.strip():
            raise ValueError("workspace_id must be a non-empty id without surrounding whitespace")
        if not self.allowed_roles or not self.allowed_roles <= KNOWN_ROLES:
            raise ValueError("allowed_roles must be a non-empty subset of Dify roles")

    @classmethod
    def from_env(cls) -> BridgeSettings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        origin = normalize_origin(required("AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN"))
        if origin is None:
            raise ValueError(
                "AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN must be an http(s) origin (scheme://host[:port])"
            )
        publish_key = (
            os.environ.get("AIOPS_CONSOLE_BRIDGE_PUBLISH_API_KEY", "").strip()
            or os.environ.get("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY", "").strip()
        )
        if not publish_key:
            raise ValueError(
                "AIOPS_CONSOLE_BRIDGE_PUBLISH_API_KEY "
                "(or legacy AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY) is required"
            )
        trial_key = (
            os.environ.get("AIOPS_CONSOLE_BRIDGE_TRIAL_API_KEY", "").strip()
            or os.environ.get("AIOPS_GATEWAY_DIFY_TRIAL_API_KEY", "").strip()
        )
        return cls(
            listen_host=os.environ.get("AIOPS_CONSOLE_BRIDGE_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("AIOPS_CONSOLE_BRIDGE_PORT", "10118")),
            dify_base_url=required("AIOPS_CONSOLE_BRIDGE_DIFY_URL").rstrip("/"),
            gateway_base_url=required("AIOPS_CONSOLE_BRIDGE_GATEWAY_URL").rstrip("/"),
            publish_key=publish_key,
            trial_key=trial_key,
            console_origin=origin,
            workspace_id=required("AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID"),
            gateway_internal_token=os.environ.get("AIOPS_CONSOLE_BRIDGE_GATEWAY_INTERNAL_TOKEN", "").strip(),
            allowed_roles=parse_allowed_roles(os.environ.get("AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES")),
            login_proxy_url=os.environ.get("AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL", "").strip().rstrip("/"),
            environment_credentials_file=os.environ.get(
                "AIOPS_CONSOLE_BRIDGE_ENVIRONMENT_CREDENTIALS_FILE", ""
            ),
            console_dist_dir=(
                os.environ.get("AIOPS_CONSOLE_BRIDGE_CONSOLE_DIST_DIR", DEFAULT_CONSOLE_DIST_DIR).strip()
                or DEFAULT_CONSOLE_DIST_DIR
            ),
        )
