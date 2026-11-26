"""快捷方式（Shortcut）与横幅（Banner）生命周期管理器。

负责租户/平台作用域鉴权、字段规则校验与草稿/发布/回滚/屏蔽恢复流程编排。
领域模型与常量位于 ``shortcut_models``，持久化存储位于 ``shortcut_store``；
本模块同时保留既有重导出以兼容路由、工具与测试调用。
"""

from __future__ import annotations

from typing import Any

from aiops_diagnostics.publication.shortcut_models import (
    _AGENT_VERSION as _AGENT_VERSION,
)
from aiops_diagnostics.publication.shortcut_models import (
    _BUNDLED_SHORTCUTS as _BUNDLED_SHORTCUTS,
)
from aiops_diagnostics.publication.shortcut_models import (
    _IMAGE_URL_MAX as _IMAGE_URL_MAX,
)
from aiops_diagnostics.publication.shortcut_models import (
    _JUMP_PATH_MAX as _JUMP_PATH_MAX,
)
from aiops_diagnostics.publication.shortcut_models import (
    _SHARED_ACTION_FIELDS as _SHARED_ACTION_FIELDS,
)
from aiops_diagnostics.publication.shortcut_models import (
    BANNER_KIND as BANNER_KIND,
)
from aiops_diagnostics.publication.shortcut_models import (
    BATTERY_REPORT_JUMP_PATH as BATTERY_REPORT_JUMP_PATH,
)
from aiops_diagnostics.publication.shortcut_models import (
    BUTTON_KIND as BUTTON_KIND,
)
from aiops_diagnostics.publication.shortcut_models import (
    EDIT_ROLES as EDIT_ROLES,
)
from aiops_diagnostics.publication.shortcut_models import (
    OPERATIONS_REVIEW_CODE as OPERATIONS_REVIEW_CODE,
)
from aiops_diagnostics.publication.shortcut_models import (
    PLATFORM_ROLES as PLATFORM_ROLES,
)
from aiops_diagnostics.publication.shortcut_models import (
    PLATFORM_SCOPE as PLATFORM_SCOPE,
)
from aiops_diagnostics.publication.shortcut_models import (
    PLATFORM_TENANT_ID as PLATFORM_TENANT_ID,
)
from aiops_diagnostics.publication.shortcut_models import (
    PUBLISH_ROLES as PUBLISH_ROLES,
)
from aiops_diagnostics.publication.shortcut_models import (
    REPORT_FAULT_JUMP_PATH as REPORT_FAULT_JUMP_PATH,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_INTENTS as SHORTCUT_INTENTS,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_KINDS as SHORTCUT_KINDS,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_MANAGE_SCOPE as SHORTCUT_MANAGE_SCOPE,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_PLATFORM_SCOPE as SHORTCUT_PLATFORM_SCOPE,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_SCOPES as SHORTCUT_SCOPES,
)
from aiops_diagnostics.publication.shortcut_models import (
    SHORTCUT_STATUSES as SHORTCUT_STATUSES,
)
from aiops_diagnostics.publication.shortcut_models import (
    STABLE_CODES as STABLE_CODES,
)
from aiops_diagnostics.publication.shortcut_models import (
    TENANT_SCOPE as TENANT_SCOPE,
)
from aiops_diagnostics.publication.shortcut_models import (
    VIEW_ROLES as VIEW_ROLES,
)
from aiops_diagnostics.publication.shortcut_models import (
    Shortcut as Shortcut,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutConflict as ShortcutConflict,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutCopyGap as ShortcutCopyGap,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutError as ShortcutError,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutForbidden as ShortcutForbidden,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutNotFound as ShortcutNotFound,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutValidationError as ShortcutValidationError,
)
from aiops_diagnostics.publication.shortcut_models import (
    ShortcutVersion as ShortcutVersion,
)
from aiops_diagnostics.publication.shortcut_models import (
    _actor as _actor,
)
from aiops_diagnostics.publication.shortcut_models import (
    _code as _code,
)
from aiops_diagnostics.publication.shortcut_models import (
    _entry as _entry,
)
from aiops_diagnostics.publication.shortcut_models import (
    _id as _id,
)
from aiops_diagnostics.publication.shortcut_models import (
    _image_url as _image_url,
)
from aiops_diagnostics.publication.shortcut_models import (
    _json as _json,
)
from aiops_diagnostics.publication.shortcut_models import (
    _jump_path as _jump_path,
)
from aiops_diagnostics.publication.shortcut_models import (
    _localized as _localized,
)
from aiops_diagnostics.publication.shortcut_models import (
    _warn_missing_translation as _warn_missing_translation,
)
from aiops_diagnostics.publication.shortcut_models import (
    logger as logger,
)
from aiops_diagnostics.publication.shortcut_models import (
    shortcut_copy_gaps as shortcut_copy_gaps,
)
from aiops_diagnostics.publication.shortcut_store import (
    ShortcutStore as ShortcutStore,
)
from aiops_diagnostics.publication.shortcut_store import (
    _shortcut_from_row as _shortcut_from_row,
)
from aiops_diagnostics.publication.shortcut_store import (
    _version_from_row as _version_from_row,
)


class ShortcutManager:
    def __init__(self, store: ShortcutStore) -> None:
        self.store = store

    def create(self, context: Any, payload: dict[str, Any], *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        fields = self._validated_fields(payload, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.create(
            tenant_id,
            _entry(payload.get("business_entry")),
            _code(payload.get("code")),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            kind=fields["kind"],
            image_url=fields["image_url"],
            created_by=_actor(context),
        )

    def list(self, context: Any, *, business_entry: str | None, scope: str = TENANT_SCOPE) -> list[Shortcut]:
        """Management listing (all statuses) for the caller's tenant+entry."""
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.list_all(tenant_id, _entry(business_entry))

    def list_published(self, context: Any, *, business_entry: str) -> list[Shortcut]:
        """Public listing — authenticated callers, roles NOT required (#230).

        Any caller that passed the endpoint's auth scope may read the entry's
        published shortcuts (they render the product home). Only drafts,
        disabled rows, other tenants, and other entries are withheld.
        """
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_published(context.effective_tenant_id, entry)

    def list_effective(self, context: Any, *, business_entry: str, kind: str | None = None) -> list[Shortcut]:
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_effective(context.effective_tenant_id, entry, kind=kind)

    def get(self, context: Any, shortcut_id: str, *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.get(_id(shortcut_id), tenant_id)

    def update(
        self, context: Any, shortcut_id: str, payload: dict[str, Any], *, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        # Absent ``jump_path`` means "leave it alone", not "clear it". The HTTP
        # request model defaults the field to None, so an unchanged edit would
        # otherwise silently demote a jump action to a prompt action — turning
        # the one field that discriminates the two into collateral damage of an
        # unrelated label edit. Explicit null clears it; both spellings go
        # through the single _jump_path validator so there is one normalizer.
        current = self.store.get(_id(shortcut_id), tenant_id)
        fields = self._validated_fields(
            {
                **payload,
                "jump_path": payload.get("jump_path") or current.jump_path,
                # Same "absent means leave it alone" rule as jump_path, and for
                # the same reason: the HTTP model defaults to None, so an
                # unrelated label edit would otherwise wipe the banner's
                # fallback image. `kind` is not in this merge because it is not
                # editable at all — see the store's update().
                "image_url": payload.get("image_url") or current.image_url,
                "kind": current.kind,
            },
            for_publish=False,
        )
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.update(
            _id(shortcut_id),
            tenant_id,
            int(payload.get("expected_revision") or 0),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            image_url=fields["image_url"],
        )

    def publish(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        shortcut = self.store.get(_id(shortcut_id), tenant_id)
        if shortcut.revision != expected_revision or shortcut.status != "draft":
            raise ShortcutConflict("shortcut revision or state changed")
        snapshot = shortcut.to_dict()
        return self.store.publish(
            shortcut.shortcut_id,
            tenant_id,
            expected_revision,
            snapshot,
            _actor(context),
        )

    def fork_draft(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        return self.store.fork_draft(_id(shortcut_id), tenant_id, expected_revision)

    def suppress(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Publish a tenant-only disabled override for a platform action."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        entry = _entry(business_entry)
        stable_code = _code(code)
        existing = self.store.find_by_code(tenant_id, entry, stable_code)
        if existing is not None:
            if existing.status == "disabled":
                return existing
            if existing.status != "published":
                raise ShortcutConflict("shortcut override is not published")
            return self.store.disable(existing.shortcut_id, tenant_id, existing.revision)
        platform = self.store.find_by_code(SHORTCUT_PLATFORM_SCOPE, entry, stable_code)
        if platform is None or platform.status != "published":
            raise ShortcutNotFound("platform shortcut not found")
        created = self.store.create(
            tenant_id,
            entry,
            stable_code,
            intent=platform.intent,
            requires_order=platform.requires_order,
            sort_order=platform.sort_order,
            labels=platform.labels,
            descriptions=platform.descriptions,
            question_templates=platform.question_templates,
            target_agent_version=None,
            jump_path=platform.jump_path,
            # The override must land on the SAME surface as the row it
            # shadows. Suppressing a banner by creating a button here would
            # both fail to hide the banner and inject a stray button.
            kind=platform.kind,
            image_url=platform.image_url,
            created_by=_actor(context),
        )
        self.publish(context, created.shortcut_id, expected_revision=created.revision)
        published = self.store.get(created.shortcut_id, tenant_id)
        return self.store.disable(published.shortcut_id, tenant_id, published.revision)

    def restore(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Restore the previous tenant override after a tenant suppression."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        existing = self.store.find_by_code(tenant_id, _entry(business_entry), _code(code))
        if existing is None:
            raise ShortcutNotFound("tenant shortcut override not found")
        if existing.status != "disabled":
            return existing
        return self.store.enable(existing.shortcut_id, tenant_id, existing.revision)

    def rollback(
        self,
        context: Any,
        shortcut_id: str,
        *,
        version_no: int,
        expected_revision: int,
        scope: str = TENANT_SCOPE,
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        current = self.store.get(_id(shortcut_id), tenant_id)
        if current.status != "published" or current.revision != expected_revision:
            raise ShortcutConflict("shortcut revision or state changed")
        target = self.store.version(current.shortcut_id, tenant_id, version_no)
        fields = self._validated_fields(target.snapshot, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        draft = self.store.fork_draft(current.shortcut_id, tenant_id, expected_revision)
        updated = self.store.update(
            draft.shortcut_id,
            tenant_id,
            draft.revision,
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            image_url=fields["image_url"],
        )
        return self.publish(context, updated.shortcut_id, expected_revision=updated.revision, scope=scope)

    def disable(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        return self.store.disable(_id(shortcut_id), tenant_id, expected_revision)

    def delete(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> None:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        self.store.delete_draft(_id(shortcut_id), tenant_id, expected_revision)

    def version(
        self, context: Any, shortcut_id: str, version_no: int, *, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        return self.store.version(_id(shortcut_id), tenant_id, version_no)

    @staticmethod
    def _scope_tenant(context: Any, scope: str, roles: frozenset[str]) -> str:
        if scope not in SHORTCUT_SCOPES:
            raise ShortcutValidationError("scope is invalid")
        if scope == PLATFORM_SCOPE:
            ShortcutManager._require(context, roles | PLATFORM_ROLES)
            if not frozenset(getattr(context, "roles", ())).intersection(PLATFORM_ROLES):
                raise ShortcutForbidden("platform shortcut access is not permitted")
            return SHORTCUT_PLATFORM_SCOPE
        ShortcutManager._require(context, roles)
        if getattr(context, "effective_tenant_id", "") == SHORTCUT_PLATFORM_SCOPE:
            raise ShortcutValidationError("tenant id is reserved")
        return context.effective_tenant_id

    @staticmethod
    def _require(context: Any, roles: frozenset[str]) -> None:
        effective_tenant = getattr(context, "effective_tenant_id", "")
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_tenant or not effective_roles.intersection(roles):
            raise ShortcutForbidden("shortcut access is not permitted")

    def _validated_fields(self, payload: dict[str, Any], *, for_publish: bool) -> dict[str, Any]:
        del for_publish  # publish-time validation equals create/update: same rules
        intent = str(payload.get("intent") or "")
        if intent not in SHORTCUT_INTENTS:
            raise ShortcutValidationError("intent is invalid")
        requires_order = bool(payload.get("requires_order", False))
        if intent in {"order_issue", "smart_diagnosis"} and not requires_order:
            # smart_diagnosis / any order_issue shortcut must force the picker.
            raise ShortcutValidationError("order-related shortcuts must set requires_order")
        sort_order = payload.get("sort_order", 100)
        if not isinstance(sort_order, int) or not 0 <= sort_order <= 9999:
            raise ShortcutValidationError("sort_order is invalid")
        labels = _localized(payload.get("labels"), "labels", require_zh=True)
        descriptions = _localized(payload.get("descriptions"), "descriptions", require_zh=False)
        question_templates = _localized(
            payload.get("question_templates"), "question_templates", require_zh=False
        )
        jump_path = _jump_path(payload.get("jump_path"))
        image_url = _image_url(payload.get("image_url"))
        kind = str(payload.get("kind") or BUTTON_KIND)
        if kind not in SHORTCUT_KINDS:
            raise ShortcutValidationError("kind is invalid")
        if kind == BANNER_KIND and jump_path is None:
            # A banner IS a jump action: the product's banner navigates to the
            # report page. A banner with no route would render a card that does
            # nothing, and it would be creatable only by misconfiguration.
            raise ShortcutValidationError("a banner must set jump_path")
        if kind == BUTTON_KIND and image_url is not None:
            # One discriminator, not two. A button carrying an image would
            # satisfy the "image ⇒ card" rule a client may still hold, so the
            # row would be served as a button by `kind` and rendered as a card
            # by the client. Rejected rather than silently dropped.
            raise ShortcutValidationError("image_url is only allowed for a banner")
        target = payload.get("target_agent_version")
        if target is not None:
            if not isinstance(target, str) or not _AGENT_VERSION.fullmatch(target):
                raise ShortcutValidationError("target_agent_version is invalid")
            if intent not in {"case_exploration", "solution_discovery"}:
                raise ShortcutValidationError("target_agent_version is only allowed for promotional intents")
        if jump_path is not None and target is not None:
            # A jump action never reaches the agent, so a pin would be dead
            # config with a live side effect: the pin is what marks an agent
            # promotional, so it would keep excluding that agent from
            # customer-agent selection for a response nobody ever fetches.
            #
            # Rejected only for NEW configuration. An old published version that
            # carries both must still be rollback-able, so this is enforced in
            # _reject_new_conflicts rather than here.
            raise ShortcutValidationError("jump_path and target_agent_version are mutually exclusive")
        return {
            "intent": intent,
            "requires_order": requires_order,
            "sort_order": sort_order,
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target,
            "jump_path": jump_path,
            "kind": kind,
            "image_url": image_url,
        }
