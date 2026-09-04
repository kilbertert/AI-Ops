"""平台隔离的固定问答目录与身份判定。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

import pymysql

from aiops_diagnostics.config import Settings
from aiops_diagnostics.scope_context import ScopeContext

PLATFORM_CONSUMER = "consumer"
PLATFORM_OPERATOR = "operator"
PLATFORMS = (PLATFORM_CONSUMER, PLATFORM_OPERATOR)
DEFAULT_FAQ_VERSION = "2026.09.04"
FAQ_NOT_FOUND = "FAQ_NOT_FOUND"
PLATFORM_AMBIGUOUS = "PLATFORM_AMBIGUOUS"
PLATFORM_FORBIDDEN = "PLATFORM_FORBIDDEN"
PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"

_LOGGER = logging.getLogger("aiops.faq")
_SAFE_DATABASE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class FAQError(RuntimeError):
    def __init__(self, message: str, *, code: str = FAQ_NOT_FOUND) -> None:
        super().__init__(message)
        self.code = code


class PlatformDirectoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlatformRoleRecord:
    b_user_id: str
    c_user_id: str | None
    tenant_id: str
    client_type: str | None


class PlatformDirectory(Protocol):
    def roles_for_c_user(self, c_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]: ...

    def roles_for_b_user(self, b_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]: ...


class MySQLPlatformDirectory:
    """只读查询真实 UPMS 身份关系；不保存或返回凭据。"""

    def __init__(self, settings: Settings, *, database: str = "qumall_upms") -> None:
        if not _SAFE_DATABASE.fullmatch(database):
            raise ValueError("UPMS database name is invalid")
        self.settings = settings
        self.database = database

    def roles_for_c_user(self, c_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]:
        return self._query("u.user_id=%s", (c_user_id, tenant_id))

    def roles_for_b_user(self, b_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]:
        return self._query("u.id=%s", (b_user_id, tenant_id))

    def _query(self, identity_where: str, identity: tuple[str, str]) -> tuple[PlatformRoleRecord, ...]:
        cfg = self.settings.mysql
        if not cfg.user or not cfg.password:
            raise PlatformDirectoryError("UPMS read-only credentials are not configured")
        try:
            connection = pymysql.connect(
                host=cfg.host,
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                database=self.database,
                charset="utf8mb4",
                connect_timeout=self.settings.upms.timeout_seconds,
                read_timeout=self.settings.upms.timeout_seconds,
                write_timeout=self.settings.upms.timeout_seconds,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                server_public_key=True,
            )
        except Exception as exc:
            raise PlatformDirectoryError("UPMS database is unavailable") from exc
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT u.id AS b_user_id, u.user_id AS c_user_id, u.tenant_id,
                           r.client_type
                    FROM `{self.database}`.`sys_user` AS u
                    LEFT JOIN `{self.database}`.`sys_user_role` AS ur
                      ON ur.user_id = u.id AND ur.tenant_id = u.tenant_id
                    LEFT JOIN `{self.database}`.`sys_role` AS r
                      ON r.id = ur.role_id
                     AND (r.tenant_id = ur.tenant_id OR r.tenant_id = '-1' OR r.tenant_id IS NULL)
                     AND (r.del_flag IS NULL OR r.del_flag = 0)
                    WHERE {identity_where} AND u.tenant_id=%s
                      AND (u.del_flag IS NULL OR u.del_flag = 0)
                    """,
                    identity,
                )
                return tuple(
                    PlatformRoleRecord(
                        b_user_id=str(row["b_user_id"]),
                        c_user_id=str(row["c_user_id"]) if row.get("c_user_id") else None,
                        tenant_id=str(row["tenant_id"]),
                        client_type=str(row["client_type"]).strip() if row.get("client_type") else None,
                    )
                    for row in cursor.fetchall()
                )
        except Exception as exc:
            raise PlatformDirectoryError("UPMS identity query failed") from exc
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


@dataclass(frozen=True, slots=True)
class PlatformDecision:
    platform: str
    available_platforms: tuple[str, ...]
    b_subject_ids: tuple[str, ...]
    reason: str

    def public(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "available_platforms": list(self.available_platforms),
        }


class PlatformIdentityResolver:
    def __init__(
        self,
        directory: PlatformDirectory,
        *,
        operator_client_types: tuple[str, ...] = ("admin", "tenant-app", "MA", "supply-admin"),
    ) -> None:
        self.directory = directory
        self.operator_client_types = frozenset(item.strip() for item in operator_client_types if item.strip())

    def resolve(self, context: ScopeContext, entry: str | None = None) -> PlatformDecision:
        entry = (entry or "").strip().lower() or None
        if entry not in {None, *PLATFORMS}:
            raise FAQError("business entry is invalid", code=PLATFORM_FORBIDDEN)
        tenant_id = context.effective_tenant_id
        c_user_id = context.subject.c_user_id
        records = (
            self.directory.roles_for_c_user(c_user_id, tenant_id)
            if c_user_id
            else self.directory.roles_for_b_user(context.subject.b_user_id, tenant_id)
        )
        for record in records:
            if record.tenant_id != tenant_id:
                raise FAQError("identity mapping crosses tenant boundary", code=PLATFORM_UNAVAILABLE)
        operator_records = tuple(
            record for record in records if record.client_type in self.operator_client_types
        )
        available: list[str] = []
        if c_user_id:
            available.append(PLATFORM_CONSUMER)
        if operator_records:
            available.append(PLATFORM_OPERATOR)
        available_platforms = tuple(platform for platform in PLATFORMS if platform in available)

        if entry == PLATFORM_CONSUMER:
            if not c_user_id:
                raise FAQError("consumer identity is unavailable", code=PLATFORM_FORBIDDEN)
            decision = PlatformDecision(PLATFORM_CONSUMER, available_platforms, (), "consumer_entry")
        elif entry == PLATFORM_OPERATOR:
            subject_ids = tuple(sorted({record.b_user_id for record in operator_records}))
            if not operator_records:
                raise FAQError("operator identity is unavailable", code=PLATFORM_UNAVAILABLE)
            if len(subject_ids) != 1:
                raise FAQError("operator identity is ambiguous", code=PLATFORM_AMBIGUOUS)
            decision = PlatformDecision(PLATFORM_OPERATOR, available_platforms, subject_ids, "operator_entry")
        elif len(available_platforms) != 1:
            raise FAQError("business entry is required to select a platform", code=PLATFORM_AMBIGUOUS)
        else:
            decision = PlatformDecision(available_platforms[0], available_platforms, (), "唯一可用平台")

        _LOGGER.info(
            "faq_platform_decision platform=%s available=%s subject_hash=%s tenant_hash=%s reason=%s",
            decision.platform,
            ",".join(decision.available_platforms),
            _hash(context.subject.b_user_id),
            _hash(tenant_id),
            decision.reason,
        )
        return decision


class FAQCatalog:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.version = str(payload.get("faq_version") or DEFAULT_FAQ_VERSION)
        raw_platforms = payload.get("platforms")
        if not isinstance(raw_platforms, dict):
            raise ValueError("FAQ catalog platforms are invalid")
        self._entries: dict[str, dict[str, dict[str, Any]]] = {}
        for platform in PLATFORMS:
            entries = raw_platforms.get(platform)
            if not isinstance(entries, list):
                raise ValueError(f"FAQ catalog is missing {platform}")
            parsed: dict[str, dict[str, Any]] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("FAQ catalog entry is invalid")
                question_id = str(entry.get("question_id") or "")
                if not question_id.startswith(f"{platform}.") or question_id in parsed:
                    raise ValueError("FAQ question_id is invalid or duplicated")
                question = str(entry.get("question") or "")
                answer = str(entry.get("answer") or "")
                if not question or not answer:
                    raise ValueError("FAQ question and answer are required")
                parsed[question_id] = {
                    "question_id": question_id,
                    "question": question,
                    "answer": answer,
                    "format": "text",
                }
            self._entries[platform] = parsed

    @classmethod
    def bundled(cls) -> FAQCatalog:
        raw = files("aiops_diagnostics").joinpath("faq_catalog.json").read_text(encoding="utf-8")
        return cls(json.loads(raw))

    def recommendations(self, platform: str) -> list[dict[str, Any]]:
        return [
            {"question_id": entry["question_id"], "title": entry["question"], "sort": index}
            for index, entry in enumerate(self._entries[platform].values(), 1)
        ]

    def catalog(self, platform: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._entries[platform].values()]

    def answer(self, platform: str, question_id: str) -> dict[str, Any]:
        if not isinstance(question_id, str) or not question_id.startswith(f"{platform}."):
            raise FAQError("FAQ question was not found")
        entry = self._entries[platform].get(question_id)
        if entry is None:
            raise FAQError("FAQ question was not found")
        return dict(entry)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
