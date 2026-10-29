from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_AMBIGUOUS_SUBJECT,
    SCOPE_ERROR_AUTH_FAILED,
    SCOPE_ERROR_DELEGATION_DENIED,
    SCOPE_ERROR_EMPTY_SCOPE,
    SCOPE_ERROR_SUBJECT_NOT_FOUND,
    SCOPE_ERROR_TENANT_FORBIDDEN,
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    RoleGrant,
    ScopeContext,
    ScopeError,
    ScopePolicy,
    ScopeRequest,
    ScopeResolver,
    SubjectRecord,
    UserContext,
)

VALID_CREDENTIAL = "platform-token-ops-1"
DELEGATED_CREDENTIAL = "platform-token-support-1"


def _caller_record(**overrides: Any) -> SubjectRecord:
    fields: dict[str, Any] = {
        "b_user_id": "B-CALLER-1",
        "c_user_id": "C-CALLER-1",
        "username": "ops.caller",
        "tenant_id": "TENANT-A",
        "organ_id": "ORG-A-1",
        "shop_id": "SHOP-A-1",
    }
    fields.update(overrides)
    return SubjectRecord(**fields)


def _caller(**overrides: Any) -> UserContext:
    subject = overrides.pop("subject", None) or _caller_record()
    roles = overrides.pop("roles", (RoleGrant(code="ROLE_OPS"),))
    permissions = overrides.pop("permissions", frozenset({"order:diag:view"}))
    return UserContext(subject=subject, roles=roles, permissions=permissions)


def _target_record(**overrides: Any) -> SubjectRecord:
    fields: dict[str, Any] = {
        "b_user_id": "B-TARGET-2",
        "c_user_id": "C-TARGET-2",
        "username": "merchant.target",
        "tenant_id": "TENANT-A",
        "organ_id": "ORG-A-2",
        "shop_id": "SHOP-A-2",
    }
    fields.update(overrides)
    return SubjectRecord(**fields)


def _scope(**overrides: Any) -> DataScope:
    fields: dict[str, Any] = {
        "type": SCOPE_TYPE_ORGAN,
        "organ_ids": ("ORG-A-1",),
        "shop_ids": ("SHOP-A-1",),
        "site_ids": ("SITE-A-1",),
    }
    fields.update(overrides)
    return DataScope(**fields)


class _FakeDirectory:
    """In-memory platform directory recording every resolution call."""

    def __init__(
        self,
        caller: UserContext,
        *,
        data_scope: DataScope | None = None,
        by_b_user_id: dict[str, SubjectRecord] | None = None,
        by_c_user_id: dict[str, tuple[SubjectRecord, ...]] | None = None,
        caller_by_credential: dict[str, UserContext] | None = None,
        unavailable: str | None = None,
    ) -> None:
        self.caller = caller
        self.business_scope = data_scope if data_scope is not None else _scope()
        self.by_b_user_id = by_b_user_id or {}
        self.by_c_user_id = by_c_user_id or {}
        self.caller_by_credential = caller_by_credential or {VALID_CREDENTIAL: caller}
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    def user_info(self, credential: str) -> UserContext:
        self.calls.append(("user_info", credential))
        if self.unavailable:
            raise ScopeError(f"UPMS 不可用: {self.unavailable}", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        context = self.caller_by_credential.get(credential)
        if context is None:
            raise ScopeError("平台凭证无效或已过期", code=SCOPE_ERROR_AUTH_FAILED)
        return context

    def user_by_b_user_id(self, credential: str, b_user_id: str) -> SubjectRecord | None:
        self.calls.append(("user_by_b_user_id", b_user_id))
        if self.unavailable:
            raise ScopeError(f"UPMS 不可用: {self.unavailable}", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        return self.by_b_user_id.get(b_user_id)

    def users_by_c_user_id(self, credential: str, c_user_id: str) -> tuple[SubjectRecord, ...]:
        self.calls.append(("users_by_c_user_id", c_user_id))
        if self.unavailable:
            raise ScopeError(f"UPMS 不可用: {self.unavailable}", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        return self.by_c_user_id.get(c_user_id, ())

    def data_scope(self, credential: str) -> DataScope:
        self.calls.append(("data_scope", credential))
        if self.unavailable:
            raise ScopeError(f"UPMS 不可用: {self.unavailable}", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        if credential not in self.caller_by_credential:
            raise ScopeError("平台凭证无效或已过期", code=SCOPE_ERROR_AUTH_FAILED)
        return self.business_scope


def _policy() -> ScopePolicy:
    return ScopePolicy(
        delegated_lookup_permissions=frozenset({"user:delegate:view"}),
        tenant_admin_roles=frozenset({"ROLE_PLATFORM_ADMIN"}),
    )


def _resolver(directory: _FakeDirectory) -> ScopeResolver:
    return ScopeResolver(directory, policy=_policy())


def _resolve(directory: _FakeDirectory, **request: Any) -> ScopeContext:
    return _resolver(directory).resolve(ScopeRequest(credential=VALID_CREDENTIAL, **request))


def test_resolver_resolves_caller_identity_and_scope_without_target() -> None:
    directory = _FakeDirectory(_caller())
    context = _resolve(directory)

    assert context.caller.b_user_id == "B-CALLER-1"
    assert context.subject == context.caller
    assert context.delegated is False
    assert context.effective_tenant_id == "TENANT-A"
    assert context.roles == frozenset({"ROLE_OPS"})
    assert context.permissions == frozenset({"order:diag:view"})
    assert context.data_scope == _scope()
    assert context.resolved_at is not None
    assert context.scope_fingerprint
    assert directory.calls[0] == ("user_info", VALID_CREDENTIAL)
    assert ("data_scope", VALID_CREDENTIAL) in directory.calls


def test_resolver_keeps_target_subject_separate_from_caller() -> None:
    delegated = _caller(permissions=frozenset({"user:delegate:view"}))
    directory = _FakeDirectory(
        delegated,
        caller_by_credential={DELEGATED_CREDENTIAL: delegated},
        by_b_user_id={"B-TARGET-2": _target_record()},
    )

    context = ScopeResolver(directory, policy=_policy()).resolve(
        ScopeRequest(credential=DELEGATED_CREDENTIAL, target_b_user_id="B-TARGET-2")
    )

    assert context.caller.b_user_id == "B-CALLER-1"
    assert context.subject.b_user_id == "B-TARGET-2"
    assert context.subject.c_user_id == "C-TARGET-2"
    assert context.subject.tenant_id == "TENANT-A"
    assert context.delegated is True
    assert ("user_by_b_user_id", "B-TARGET-2") in directory.calls


def test_resolver_maps_c_user_id_to_single_b_user() -> None:
    directory = _FakeDirectory(
        _caller(permissions=frozenset({"user:delegate:view"})),
        by_c_user_id={"C-TARGET-2": (_target_record(),)},
    )

    context = _resolve(directory, target_c_user_id="C-TARGET-2")

    assert context.subject.b_user_id == "B-TARGET-2"
    assert context.subject.c_user_id == "C-TARGET-2"
    assert context.delegated is True
    assert ("users_by_c_user_id", "C-TARGET-2") in directory.calls


def test_resolver_fails_closed_when_c_user_id_has_no_mapping() -> None:
    directory = _FakeDirectory(_caller(permissions=frozenset({"user:delegate:view"})))

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_c_user_id="C-UNKNOWN")

    assert excinfo.value.code == SCOPE_ERROR_SUBJECT_NOT_FOUND


def test_resolver_fails_closed_when_c_user_id_maps_to_multiple_b_users() -> None:
    directory = _FakeDirectory(
        _caller(permissions=frozenset({"user:delegate:view"})),
        by_c_user_id={"C-AMBIG": (_target_record(), _target_record(b_user_id="B-TARGET-3"))},
    )

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_c_user_id="C-AMBIG")

    assert excinfo.value.code == SCOPE_ERROR_AMBIGUOUS_SUBJECT


def test_resolver_fails_closed_when_b_user_id_is_unknown() -> None:
    directory = _FakeDirectory(_caller(permissions=frozenset({"user:delegate:view"})))

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_b_user_id="B-UNKNOWN")

    assert excinfo.value.code == SCOPE_ERROR_SUBJECT_NOT_FOUND


def test_resolver_rejects_both_target_selectors() -> None:
    directory = _FakeDirectory(_caller(permissions=frozenset({"user:delegate:view"})))

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_b_user_id="B-TARGET-2", target_c_user_id="C-TARGET-2")

    assert excinfo.value.code == SCOPE_ERROR_AMBIGUOUS_SUBJECT
    assert directory.calls == []


def test_resolver_requires_delegation_permission_before_target_lookup() -> None:
    directory = _FakeDirectory(
        _caller(),
        by_b_user_id={"B-TARGET-2": _target_record()},
    )

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_b_user_id="B-TARGET-2")

    assert excinfo.value.code == SCOPE_ERROR_DELEGATION_DENIED
    assert ("user_by_b_user_id", "B-TARGET-2") not in directory.calls
    assert ("data_scope", VALID_CREDENTIAL) not in directory.calls


def test_resolver_allows_delegated_target_for_platform_admin_role() -> None:
    admin = _caller(
        roles=(RoleGrant(code="ROLE_OPS_SUPER", parent_codes=("ROLE_PLATFORM_ADMIN",)),),
        permissions=frozenset({"order:diag:view"}),
    )
    directory = _FakeDirectory(admin, by_b_user_id={"B-TARGET-2": _target_record()})

    context = _resolve(directory, target_b_user_id="B-TARGET-2")

    assert context.delegated is True


def test_resolver_rejects_tenant_widening_for_normal_caller() -> None:
    directory = _FakeDirectory(_caller())

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, tenant_id="TENANT-B")

    assert excinfo.value.code == SCOPE_ERROR_TENANT_FORBIDDEN


def test_resolver_honors_explicit_tenant_switch_for_platform_admin() -> None:
    admin = _caller(
        roles=(RoleGrant(code="ROLE_PLATFORM_ADMIN"),),
        subject=_caller_record(tenant_id=None),
    )
    directory = _FakeDirectory(admin)

    context = _resolve(directory, tenant_id="TENANT-B")

    assert context.effective_tenant_id == "TENANT-B"
    assert context.caller.tenant_id is None


def test_resolver_uses_target_tenant_when_admin_delegates_cross_tenant() -> None:
    admin = _caller(roles=(RoleGrant(code="ROLE_PLATFORM_ADMIN"),))
    directory = _FakeDirectory(
        admin,
        by_b_user_id={"B-TARGET-9": _target_record(tenant_id="TENANT-B")},
    )

    context = _resolve(directory, target_b_user_id="B-TARGET-9")

    assert context.effective_tenant_id == "TENANT-B"


def test_resolver_rejects_cross_tenant_target_without_admin_role() -> None:
    delegated = _caller(permissions=frozenset({"user:delegate:view"}))
    directory = _FakeDirectory(
        delegated,
        by_b_user_id={"B-TARGET-9": _target_record(tenant_id="TENANT-B")},
    )

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_b_user_id="B-TARGET-9")

    assert excinfo.value.code == SCOPE_ERROR_TENANT_FORBIDDEN


def test_resolver_treats_blank_tenant_condition_as_absent() -> None:
    directory = _FakeDirectory(_caller())

    context = _resolve(directory, tenant_id="  ")

    assert context.effective_tenant_id == "TENANT-A"


def test_resolver_fails_closed_on_empty_business_scope() -> None:
    empty_scope = _scope(type=SCOPE_TYPE_ORGAN, organ_ids=(), shop_ids=(), site_ids=())
    directory = _FakeDirectory(_caller(), data_scope=empty_scope)

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory)

    assert excinfo.value.code == SCOPE_ERROR_EMPTY_SCOPE


def test_resolver_fails_closed_when_tenant_cannot_be_determined() -> None:
    directory = _FakeDirectory(
        _caller(subject=_caller_record(tenant_id=None)),
        data_scope=_scope(),
    )

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory)

    assert excinfo.value.code == SCOPE_ERROR_EMPTY_SCOPE


def test_resolver_allows_self_and_all_scope_types() -> None:
    self_scope = _FakeDirectory(_caller(), data_scope=_scope(type=SCOPE_TYPE_SELF))
    assert _resolve(self_scope).data_scope.type == SCOPE_TYPE_SELF

    all_scope = _scope(type=SCOPE_TYPE_ALL, organ_ids=(), shop_ids=(), site_ids=())
    every_tenant = _FakeDirectory(_caller(), data_scope=all_scope)
    assert _resolve(every_tenant).data_scope.type == SCOPE_TYPE_ALL


def test_resolver_fails_closed_when_upms_is_unavailable() -> None:
    directory = _FakeDirectory(_caller(), unavailable="connection refused")

    with pytest.raises(ScopeError) as excinfo:
        _resolve(directory, target_b_user_id="B-TARGET-2")

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE
    assert directory.calls == [("user_info", VALID_CREDENTIAL)]


def test_resolver_fails_closed_on_invalid_credential() -> None:
    directory = _FakeDirectory(_caller())

    with pytest.raises(ScopeError) as excinfo:
        _resolver(directory).resolve(ScopeRequest(credential="platform-token-attacker-1"))

    assert excinfo.value.code == SCOPE_ERROR_AUTH_FAILED


def test_resolver_rejects_blank_credential_without_directory_call() -> None:
    directory = _FakeDirectory(_caller())

    with pytest.raises(ScopeError) as excinfo:
        _resolver(directory).resolve(ScopeRequest(credential="   "))

    assert excinfo.value.code == SCOPE_ERROR_AUTH_FAILED
    assert directory.calls == []


def test_resolver_expands_inherited_roles_into_effective_role_set() -> None:
    caller = _caller(
        roles=(
            RoleGrant(code="ROLE_OPS_SUPER", parent_codes=("ROLE_OPS",)),
            RoleGrant(code="ROLE_OPS", parent_codes=("ROLE_BASE",)),
        )
    )
    directory = _FakeDirectory(caller)

    context = _resolve(directory)

    assert context.roles == frozenset({"ROLE_OPS_SUPER", "ROLE_OPS", "ROLE_BASE"})


def test_inherited_admin_role_grants_tenant_switch() -> None:
    admin = _caller(
        roles=(RoleGrant(code="ROLE_PLATFORM_OWNER", parent_codes=("ROLE_PLATFORM_ADMIN",)),),
        subject=_caller_record(tenant_id=None),
    )
    directory = _FakeDirectory(admin)

    context = _resolve(directory, tenant_id="TENANT-C")

    assert context.effective_tenant_id == "TENANT-C"


def test_scope_context_is_immutable() -> None:
    directory = _FakeDirectory(_caller())
    context = _resolve(directory)

    with pytest.raises(FrozenInstanceError):
        context.effective_tenant_id = "TENANT-B"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.subject = _target_record()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.data_scope.organ_ids = ("ORG-B-1",)  # type: ignore[misc]


def test_scope_context_fingerprint_is_deterministic_and_scope_sensitive() -> None:
    first = _resolve(_FakeDirectory(_caller()))
    second = _resolve(_FakeDirectory(_caller()))
    assert first.scope_fingerprint == second.scope_fingerprint

    wider = _resolve(_FakeDirectory(_caller()), tenant_id="TENANT-A")
    assert wider.scope_fingerprint == first.scope_fingerprint

    delegated = _resolve(
        _FakeDirectory(
            _caller(permissions=frozenset({"user:delegate:view"})),
            by_b_user_id={"B-TARGET-2": _target_record()},
        ),
        target_b_user_id="B-TARGET-2",
    )
    assert delegated.scope_fingerprint != first.scope_fingerprint

    narrowed_scope = _scope(organ_ids=("ORG-A-9",), shop_ids=("SHOP-A-1",), site_ids=("SITE-A-1",))
    narrowed = _resolve(_FakeDirectory(_caller(), data_scope=narrowed_scope))
    assert narrowed.scope_fingerprint != first.scope_fingerprint

    promoted = _resolve(_FakeDirectory(_caller(roles=(RoleGrant(code="ROLE_PLATFORM_ADMIN"),))))
    assert promoted.scope_fingerprint != first.scope_fingerprint


def test_scope_context_rejects_inconsistent_fingerprint() -> None:
    context = _resolve(_FakeDirectory(_caller()))

    with pytest.raises(ValueError):
        ScopeContext(
            caller=context.caller,
            subject=context.subject,
            delegated=context.delegated,
            effective_tenant_id=context.effective_tenant_id,
            data_scope=context.data_scope,
            roles=context.roles,
            permissions=context.permissions,
            resolved_at=context.resolved_at,
            scope_fingerprint="0" * 64,
        )


def test_audit_summary_omits_credentials_and_permission_copies() -> None:
    delegated = _caller(permissions=frozenset({"user:delegate:view"}))
    context = _resolve(
        _FakeDirectory(delegated, by_b_user_id={"B-TARGET-2": _target_record()}),
        target_b_user_id="B-TARGET-2",
    )

    summary = context.audit_summary()
    serialized = json.dumps(summary, ensure_ascii=False, default=str)

    assert summary["caller_b_user_id"] == "B-CALLER-1"
    assert summary["subject_b_user_id"] == "B-TARGET-2"
    assert summary["subject_c_user_id"] == "C-TARGET-2"
    assert summary["delegated"] is True
    assert summary["effective_tenant_id"] == "TENANT-A"
    assert summary["scope_fingerprint"] == context.scope_fingerprint
    assert summary["resolved_at"]
    assert "permissions" not in summary
    assert "roles" not in serialized
    assert VALID_CREDENTIAL not in serialized


def test_scope_context_never_carries_the_platform_credential() -> None:
    context = _resolve(_FakeDirectory(_caller()))

    field_names = [item.name for item in fields(context)]
    assert not [name for name in field_names if "credential" in name or "token" in name or "secret" in name]
    assert VALID_CREDENTIAL not in json.dumps(context.audit_summary(), default=str)


def test_scope_error_carries_fail_closed_code() -> None:
    error = ScopeError("权限不足", code=SCOPE_ERROR_DELEGATION_DENIED)

    assert error.code == SCOPE_ERROR_DELEGATION_DENIED
    assert str(error) == "权限不足"


def test_data_scope_rejects_unknown_scope_type() -> None:
    with pytest.raises(ValueError):
        _scope(type="unbounded")
