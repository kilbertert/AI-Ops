"""产品快捷动作、运营横幅与开场白（starters）路由。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, status

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.conversation.faq import PlatformDecision
from aiops_diagnostics.gateway.dependencies import GatewayDeps, localized_copy_language
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import (
    ShortcutActionRequest,
    ShortcutCreateRequest,
    ShortcutRollbackRequest,
    ShortcutUpdateRequest,
)
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.publication.agent_content import AGENT_STARTERS
from aiops_diagnostics.publication.shortcut_lifecycle import (
    BANNER_KIND,
    BUTTON_KIND,
    TENANT_SCOPE,
    ShortcutConflict,
    ShortcutError,
    ShortcutForbidden,
    ShortcutNotFound,
)


def _shortcut_error(exc: ShortcutError) -> StandardAPIError:
    if isinstance(exc, ShortcutNotFound):
        return StandardAPIError(status.HTTP_404_NOT_FOUND, "SHORTCUT_NOT_FOUND", "shortcut not found")
    if isinstance(exc, ShortcutForbidden):
        return StandardAPIError(
            status.HTTP_403_FORBIDDEN, "SHORTCUT_FORBIDDEN", "shortcut access is not permitted"
        )
    if isinstance(exc, ShortcutConflict):
        return StandardAPIError(
            status.HTTP_409_CONFLICT, "SHORTCUT_REVISION_CONFLICT", "shortcut revision conflict"
        )
    return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))


def _dify_starters(
    context: Any, caller: Any, business_entry: str, language: str
) -> tuple[str | None, tuple[str, ...] | None]:
    """运营在 Dify 里写的开场白，仅对 ``zh`` 且仅读取已发布快照。"""
    if language != DEFAULT_LANGUAGE:
        return None, None
    tenant_id = str(getattr(caller, "effective_tenant_id", "") or "")
    entry = str(business_entry or "").strip().lower()
    if not tenant_id or not entry:
        return None, None
    try:
        runtime = context.runtime
        store = getattr(runtime, "agent_store", None)
        registry = getattr(runtime, "dify_app_registry", None)
        if store is None or registry is None or not registry.is_configured():
            return None, None
        from aiops_diagnostics.publication.qa_rag import select_customer_agent

        registered_name, configured = runtime._registered_agent(tenant_id, entry)
        if not configured or registered_name is None:
            return None, None
        selection = select_customer_agent(store, tenant_id, agent_name=registered_name)
        if selection is None:
            return None, None
        version = store.version(selection.agent_id, tenant_id, selection.version_no)
        opening = str(version.snapshot.get("opening_statement") or "").strip()
        suggested = tuple(
            str(item).strip()
            for item in (version.snapshot.get("suggested_questions") or ())
            if str(item).strip()
        )
    except Exception:  # noqa: BLE001 - presentation must never break the page
        return None, None
    return (opening or None), (suggested or None)


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:

    @app.get("/v1/assistant/starters")
    def assistant_starters(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(deps.shortcut_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """中文母版来自 Dify 已发布快照，其余语言回退至制品文案。"""
        caller, _ = identity
        payload = AGENT_STARTERS.public(language)
        opening, suggested = _dify_starters(context, caller, business_entry or "", language)
        if opening is not None:
            payload["opening"] = opening
            payload["opening_source"] = "dify"
        else:
            payload["opening_source"] = "artifact"
        if suggested is not None:
            payload["suggested_questions"] = list(suggested)
            payload["suggested_source"] = "dify"
        else:
            payload["suggested_source"] = "artifact"
        return {"type": "assistant_starters", **payload}

    @app.get("/v1/shortcuts")
    def list_shortcuts(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(deps.shortcut_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        try:
            shortcuts = context.shortcut_manager.list_effective(
                caller, business_entry=decision.platform, kind=BUTTON_KIND
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_list",
            "language": api._shortcut_list_language(shortcuts, language),
            "count": len(shortcuts),
            "shortcuts": [item.public(language) for item in shortcuts],
        }

    @app.get("/v1/banner")
    def list_banners(
        identity: tuple[ScopeContext, PlatformDecision] = Depends(deps.shortcut_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        try:
            banners = context.shortcut_manager.list_effective(
                caller, business_entry=decision.platform, kind=BANNER_KIND
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "banner",
            "language": api._shortcut_list_language(banners, language),
            "count": len(banners),
            "banners": [item.banner(language) for item in banners],
        }

    @app.post("/v1/shortcuts", status_code=status.HTTP_201_CREATED)
    def create_shortcut(
        payload: ShortcutCreateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.create(caller, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.get("/v1/shortcut-admin/{business_entry}")
    def list_shortcut_admin(
        business_entry: str,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Management listing (all statuses, full multilingual fields)."""
        try:
            shortcuts = context.shortcut_manager.list(caller, business_entry=business_entry, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_admin_list",
            "count": len(shortcuts),
            "shortcuts": [s.to_dict() for s in shortcuts],
        }

    @app.get("/v1/shortcuts/{shortcut_id}/versions/{version_no}")
    def get_shortcut_version(
        shortcut_id: str,
        version_no: int,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.version(caller, shortcut_id, version_no, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.put("/v1/shortcuts/{shortcut_id}")
    def update_shortcut(
        shortcut_id: str,
        payload: ShortcutUpdateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.update(caller, shortcut_id, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/publish")
    def publish_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.publish(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/draft")
    def fork_shortcut_draft(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.fork_draft(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/rollback")
    def rollback_shortcut(
        shortcut_id: str,
        payload: ShortcutRollbackRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.rollback(
                caller,
                shortcut_id,
                version_no=payload.version_no,
                expected_revision=payload.expected_revision,
                scope=scope,
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/suppress")
    def suppress_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.suppress(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/restore")
    def restore_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.restore(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/disable")
    def disable_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.disable(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.delete("/v1/shortcuts/{shortcut_id}")
    def delete_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(deps.authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, bool]:
        try:
            context.shortcut_manager.delete(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {"deleted": True}
