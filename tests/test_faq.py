from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

from aiops_diagnostics.faq import (
    PLATFORM_AMBIGUOUS,
    PLATFORM_CONSUMER,
    PLATFORM_FORBIDDEN,
    PLATFORM_OPERATOR,
    FAQCatalog,
    FAQError,
    PlatformIdentityResolver,
    PlatformRoleRecord,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "faq_generator", Path(__file__).parents[1] / "tools/generate_faq_catalog.py"
)
assert _GENERATOR_SPEC and _GENERATOR_SPEC.loader
faq_generator = importlib.util.module_from_spec(_GENERATOR_SPEC)
_GENERATOR_SPEC.loader.exec_module(faq_generator)


class _Directory:
    def __init__(self, records: tuple[PlatformRoleRecord, ...] = ()) -> None:
        self.records = records

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return self.records

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return self.records


def _context(*, c_user_id: str | None = "C-1") -> ScopeContext:
    subject = SubjectRecord(b_user_id="c:C-1" if c_user_id else "B-1", c_user_id=c_user_id, tenant_id="T-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:faq:read"}),
    )


def test_consumer_identity_without_b_binding_is_available() -> None:
    decision = PlatformIdentityResolver(_Directory()).resolve(_context())
    assert decision.platform == PLATFORM_CONSUMER
    assert decision.available_platforms == (PLATFORM_CONSUMER,)


def test_operator_entry_requires_one_operator_subject() -> None:
    records = (PlatformRoleRecord("B-1", "C-1", "T-1", "ADMIN"),)
    decision = PlatformIdentityResolver(_Directory(records)).resolve(_context(), "operator")
    assert decision.platform == PLATFORM_OPERATOR
    assert decision.b_subject_ids == ("B-1",)
    assert decision.available_platforms == (PLATFORM_CONSUMER, PLATFORM_OPERATOR)


def test_multiple_operator_subjects_are_ambiguous() -> None:
    records = (
        PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),
        PlatformRoleRecord("B-2", "C-1", "T-1", "tenant-app"),
    )
    try:
        PlatformIdentityResolver(_Directory(records)).resolve(_context(), "operator")
    except FAQError as exc:
        assert exc.code == PLATFORM_AMBIGUOUS
    else:
        raise AssertionError("expected ambiguous platform")


def test_consumer_entry_does_not_select_operator_content() -> None:
    records = (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)
    decision = PlatformIdentityResolver(_Directory(records)).resolve(_context(), "consumer")
    assert decision.platform == PLATFORM_CONSUMER


def test_b_identity_cannot_use_consumer_entry() -> None:
    try:
        PlatformIdentityResolver(_Directory()).resolve(_context(c_user_id=None), "consumer")
    except FAQError as exc:
        assert exc.code == PLATFORM_FORBIDDEN
    else:
        raise AssertionError("expected consumer platform rejection")


def test_unknown_role_does_not_create_operator_platform() -> None:
    records = (PlatformRoleRecord("B-1", "C-1", "T-1", "unknown"),)
    decision = PlatformIdentityResolver(_Directory(records)).resolve(_context(), "consumer")
    assert decision.available_platforms == (PLATFORM_CONSUMER,)
    try:
        PlatformIdentityResolver(_Directory(records)).resolve(_context(), "operator")
    except FAQError as exc:
        assert exc.code == "PLATFORM_UNAVAILABLE"
    else:
        raise AssertionError("expected operator rejection")


def test_cross_tenant_mapping_fails_closed() -> None:
    records = (PlatformRoleRecord("B-1", "C-1", "T-OTHER", "admin"),)
    try:
        PlatformIdentityResolver(_Directory(records)).resolve(_context(), "consumer")
    except FAQError as exc:
        assert exc.code == "PLATFORM_UNAVAILABLE"
    else:
        raise AssertionError("expected cross-tenant rejection")


def test_invalid_business_entry_is_forbidden() -> None:
    try:
        PlatformIdentityResolver(_Directory()).resolve(_context(), "operator-admin")
    except FAQError as exc:
        assert exc.code == PLATFORM_FORBIDDEN
    else:
        raise AssertionError("expected invalid entry rejection")


def test_catalog_is_bundled_and_platform_prefixed() -> None:
    catalog = FAQCatalog.bundled()
    assert len(catalog.catalog("consumer")) == 28
    assert len(catalog.catalog("operator")) == 17
    assert all(item["question_id"].startswith("consumer.") for item in catalog.catalog("consumer"))
    assert all(item["question_id"].startswith("operator.") for item in catalog.catalog("operator"))
    assert all("answer" not in item for item in catalog.recommendations("consumer"))
    json.dumps(catalog.catalog("consumer"), ensure_ascii=False)


def test_catalog_generator_excludes_conflicting_answers(tmp_path: Path) -> None:
    source = tmp_path / "input.docx"
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>\
<w:p><w:r><w:t>Q：同一个问题</w:t></w:r></w:p><w:p><w:r><w:t>A：答案一</w:t></w:r></w:p>\
<w:p><w:r><w:t>Q：同一个问题</w:t></w:r></w:p><w:p><w:r><w:t>A：答案二</w:t></w:r></w:p>\
</w:body></w:document>"""
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", xml)
    entries, duplicates, conflicts = faq_generator.build("consumer", source, None)
    assert entries == []
    assert duplicates == 0
    assert conflicts == 1
