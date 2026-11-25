"""One platform-decision rule, three required scopes (#486).

The rule — "which content domain is this request in, given a VERIFIED caller and
the entry context" — had been written out three times: twice byte-for-byte, once
differing in a single line. That line was the return type (`PlatformDecision`
vs `str(decision.platform)`), and it was load-bearing: the FAQ and assistant
branches need the decision object, the shortcut branch needed the string. Each
caller had read the copy that suited its own shape, which is how a single rule
became three.

The reason this is worth a guard rather than a one-time cleanup: the copies
would answer *differently* under a future edit to any one of them, and no
existing test could see it — each copy is covered by its own endpoints' tests,
so changing one leaves the others green.

Two kinds of assertion, because they catch different things:

* **Behavioural** — the same platform failure produces the same answer on the
  FAQ, assistant and shortcut surfaces. This is what a diverging copy breaks.
* **Structural** — one definition, and the return type is the object everywhere.
  This is what a *new* copy would break, which no behaviour test today can see.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.conversation.faq import (
    FAQCatalog,
    PlatformIdentityResolver,
    PlatformRoleRecord,
)
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
DEPS_FILE = "gateway/dependencies.py"
ROUTERS_DIR = SOURCE_ROOT / "gateway" / "routers"
#: The one factory that builds the identity dependency.
FACTORY = "platform_identity"
#: The names the factory is bound to, and the scope each must carry.
IDENTITY_NAMES = ("faq_identity", "assistant_identity", "shortcut_identity")
#: One endpoint per identity, so a failure is observed on every surface. An
#: identity may back more than one endpoint (the banner and the shortcut
#: listing share one); each is listed, because "the same rule answers the same
#: way" is a claim about every endpoint that reads it, not about each name.
SURFACES = {
    "faq_identity": ("GET", "/v1/faq/recommendations"),
    "assistant_identity": ("POST", "/v1/assistant/questions"),
    "shortcut_identity": ("GET", "/v1/shortcuts"),
    # A fourth surface on the same identity: the banner shares the shortcut
    # listing's scope, so it must also share its answer to an ambiguous entry.
    "shortcut_identity/banner": ("GET", "/v1/banner"),
}


class _Runtime:
    def shutdown(self) -> None:
        pass


class _Caller:
    """A verified caller with both platform identities available."""

    def __init__(self) -> None:
        self.subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")

    def resolve(self, token: str, **kwargs: object) -> ScopeContext:
        del token, kwargs
        return ScopeContext.build(
            caller=self.subject,
            subject=self.subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:faq:read", "aiops:diagnoses:write", "aiops:orders:read"}),
        )


class _Directory:
    """Two platform roles for the same C-side user: an ambiguous request.

    Two available platforms and no `X-Business-Entry` is the decision's own
    ambiguity, which is the failure this file drives.
    """

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        del c_user_id, tenant_id
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        del b_user_id, tenant_id
        return ()


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=store,
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_Caller(),  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),  # type: ignore[arg-type]
            faq_catalog=FAQCatalog.bundled(),
        )
    )


def test_the_same_platform_failure_answers_the_same_on_every_surface(tmp_path: Path) -> None:
    """Ambiguous platform ⇒ the same status and code on FAQ, assistant, shortcut.

    Three copies meant three chances to disagree. Today they agree; this is what
    catches the edit that makes one of them stop.
    """
    client = _client(tmp_path)
    headers = {"Authorization": "Bearer token"}
    answers: dict[str, tuple[int, str]] = {}
    for name, (method, path) in SURFACES.items():
        response = client.request(
            method,
            path,
            headers=headers,
            json={"question": "q"} if method == "POST" else None,
        )
        body = response.json().get("error", {})
        answers[name] = (response.status_code, body.get("code"))
    assert len(set(answers.values())) == 1, f"the identity surfaces disagree: {answers}"
    assert set(answers.values()) == {(409, "PLATFORM_AMBIGUOUS")}, answers


def test_the_decision_object_is_what_every_surface_receives() -> None:
    """The return type is the object, on every identity.

    The shortcut branch's `str(decision.platform)` was the one-line difference
    that let the copies drift, and it is exactly the kind of "convenience"
    return that would come back. Asserted on the annotation rather than on a
    consumer, because the consumers read different fields and only the shared
    shape is the contract.
    """
    tree = ast.parse((SOURCE_ROOT / DEPS_FILE).read_text(encoding="utf-8"))
    factory = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == FACTORY
    )
    dependency = next(
        node for node in ast.walk(factory) if isinstance(node, ast.FunctionDef) and node.name == "dependency"
    )
    returns = ast.unparse(dependency.returns)
    assert returns == "tuple[ScopeContext, PlatformDecision]", returns
    body_returns = [
        ast.unparse(node.value)
        for node in ast.walk(dependency)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert body_returns == ["(caller, decision)"], body_returns


def test_the_identity_dependencies_come_from_one_factory() -> None:
    """No literal re-implementation of the rule, and every name bound to it."""
    tree = ast.parse((SOURCE_ROOT / DEPS_FILE).read_text(encoding="utf-8"))
    builder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_dependencies"
    )
    definitions = [
        node.name
        for node in ast.walk(builder)
        if isinstance(node, ast.FunctionDef) and node.name in IDENTITY_NAMES
    ]
    assert definitions == [], f"the identity rule is written out again: {definitions}"
    bindings = {
        target.id: ast.unparse(node.value)
        for node in ast.walk(builder)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in IDENTITY_NAMES
    }
    assert set(bindings) == set(IDENTITY_NAMES), bindings
    for name, value in bindings.items():
        assert value.startswith(f"{FACTORY}("), f"{name} is not built by {FACTORY}: {value}"


def test_each_identity_carries_the_scope_its_surface_requires() -> None:
    """The scope is the whole reason the three exist; a swap must not be silent."""
    tree = ast.parse((SOURCE_ROOT / DEPS_FILE).read_text(encoding="utf-8"))
    builder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_dependencies"
    )
    bindings = {
        target.id: ast.unparse(node.value)
        for node in ast.walk(builder)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in IDENTITY_NAMES
    }
    expected = {
        "faq_identity": "authenticated_faq_caller",
        "assistant_identity": "authenticated_diagnosis_caller",
        "shortcut_identity": "authenticated_shortcut_viewer",
    }
    for name, dependency in expected.items():
        assert dependency in bindings[name], f"{name} is bound to {bindings[name]}, expected {dependency}"


#: Which identity each handler must ask for, keyed by HANDLER NAME. Not by path:
#: `/v1/shortcuts` serves both the listing (which needs the platform decision)
#: and creation (which needs only the manage scope), so a path-keyed map would
#: report a correct handler as an offender. The scope differences only matter if
#: the right handler gets the right identity, and swapping two leaves every
#: other assertion here green: the three come from one factory, so they answer
#: identically on every input the tests above drive.
SURFACE_HANDLERS = {
    "faq_recommendations": "faq_identity",
    "faq_catalog": "faq_identity",
    "faq_answer": "faq_identity",
    "assistant_questions": "assistant_identity",
    "get_assistant_question": "assistant_identity",
    "stream_assistant_question_events": "assistant_identity",
    "cancel_assistant_question": "assistant_identity",
    "export_assistant_question_report": "assistant_identity",
    "submit_assistant_question_feedback": "assistant_identity",
    "regenerate_assistant_question": "assistant_identity",
    "list_assistant_questions": "assistant_identity",
    "upload_assistant_file": "assistant_identity",
    "get_assistant_file": "assistant_identity",
    "get_assistant_file_preview": "assistant_identity",
    "create_file_download_task": "assistant_identity",
    "create_conversation": "assistant_identity",
    "list_conversations": "assistant_identity",
    "get_conversation": "assistant_identity",
    "rename_conversation": "assistant_identity",
    "delete_conversation": "assistant_identity",
    "set_conversation_active_order": "assistant_identity",
    "list_shortcuts": "shortcut_identity",
    # The starters copy is shown on the same chat surface as the shortcut
    # buttons (#585): same caller, same tenant/entry resolution, so the same
    # identity. Registering it here is the map's job — the guard exists so a new
    # surface cannot pick an identity nobody looked at.
    "assistant_starters": "shortcut_identity",
    # 后台主智能体列表与 starters/shortcuts 共用同一抽屉初始化身份（#1705）。
    "assistant_agents": "shortcut_identity",
    # The banner is a product surface on the same entry, so it reads the same
    # identity: anything the shortcut listing may show, the banner may too.
    "list_banners": "shortcut_identity",
}


def _depended_identity(handler: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for default in list(handler.args.defaults) + list(handler.args.kw_defaults):
        if not isinstance(default, ast.Call):
            continue
        if getattr(default.func, "id", "") != "Depends" or not default.args:
            continue
        arg = default.args[0]
        name = arg.attr if isinstance(arg, ast.Attribute) else getattr(arg, "id", "")
        if name in IDENTITY_NAMES:
            return name
    return None


def test_each_surface_asks_for_the_identity_that_carries_its_scope() -> None:
    """The endpoints, not just the bindings: which identity each one depends on."""
    handlers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                handlers[node.name] = node
    declared = {
        name: identity
        for name, node in handlers.items()
        if (identity := _depended_identity(node)) is not None
    }
    assert declared, "no handler depends on an identity: this guard points at nothing"
    expected = {**SURFACE_HANDLERS, "upload_consumer_photo": "assistant_identity"}
    assert declared == expected, {
        "only in code": {k: v for k, v in declared.items() if expected.get(k) != v},
        "only in the map": {k: v for k, v in expected.items() if declared.get(k) != v},
    }
