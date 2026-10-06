"""The error envelope's `code` / `message` split, as a contract (#538).

`code` is English and stable so a client can branch on it; `message` is a
developer-readable note and is NOT user-facing copy — with ONE deliberate
exception, the unified-assistant QA failure path, whose `message` the client is
told to render directly.

These are contract checks, not implementation tests: they exist so a future
"let's localize all the error messages" or "let's stop localizing this one"
change has to confront the recorded decision instead of quietly making it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def test_client_branchable_codes_come_out_of_real_responses(tmp_path: Path) -> None:
    """`code` must be the value a client actually receives — so read responses.

    A test over a hardcoded list proves only that the list matches itself: it
    stays green if the server starts returning something else, which is exactly
    the break a client would feel. These come out of the HTTP surface.
    """
    client = _client(tmp_path)
    with client:
        # 404: a diagnosis that does not exist
        not_found = client.get(
            "/v1/standard/diagnoses/dx_missing",
            headers={"Authorization": "Bearer token"},
        ).json()
        # 401: no token at all
        unauthenticated = client.get("/v1/standard/diagnoses/dx_missing").json()

    codes = {"DIAGNOSIS_NOT_FOUND": not_found, "ACCESS_TOKEN_REQUIRED": unauthenticated}
    for expected, body in codes.items():
        actual = body["error"]["code"]
        assert actual == expected, f"客户端分支用的 code 变了：期望 {expected}，实得 {actual}"
        assert actual.isascii() and actual == actual.upper()


def test_the_qa_failure_message_exception_is_still_localized() -> None:
    """The one exception must stay an exception — asserted at the WIRING.

    The client renders this field directly (frontend-api-brief 场景 B), so
    removing the localization would put a Chinese sentence in front of an
    English reader while the surrounding card looked translated — the #283
    shape this whole body of work exists to remove.

    Deliberately goes through the RESPONSE BUILDER, not `_qa_user_message`:
    replacing the call site with a literal is the mutation this must catch, and
    a test that calls the helper directly cannot see it. (That mistake was made
    twice already on this workstream — #535 and #536.)
    """
    from aiops_diagnostics.gateway_api import _assistant_question_response

    row = {
        "qa_id": "qa_1",
        "question": "q",
        "status": "failed",
        "error_code": "QA_FAILED",
        "result": None,
    }
    texts = {
        _assistant_question_response(dict(row), lang)["error"]["message"]
        for lang in ("zh", "en", "de", "fr", "es", "pt")
    }
    assert len(texts) == 6, "QA 失败文案的本地化接线断了（或不再随语言变化）"
    assert all(text.strip() for text in texts)


def test_request_level_envelopes_echo_the_message_verbatim(tmp_path: Path) -> None:
    """`StandardAPIError` messages are developer notes, not copy.

    Asserted over the REAL HTTP surface across TWO error paths (404 and 401),
    each polled in three languages: the envelope echoes the message
    byte-for-byte every time. There are ~53 call sites and none carries a
    language. If one ever starts localizing, this fails and the author must
    either record a new exception in `docs/standard-api-contract.md` §5.2.1 or
    back out.
    """
    client = _client(tmp_path)
    with client:
        for path, headers, expected in (
            ("/v1/standard/diagnoses/dx_missing", {"Authorization": "Bearer token"}, "diagnosis not found"),
            ("/v1/standard/diagnoses/dx_missing", {}, "access token required"),
        ):
            bodies = [
                client.get(path, headers={**headers, "Accept-Language": lang}).json()
                for lang in ("zh", "en", "de")
            ]
            # Byte-for-byte the SAME developer string in every language, and the
            # exact string — not merely "unchanged". Asserting only that three
            # languages agree passes for any message, including a translated or
            # prefixed one, which is why the first version of this test could
            # not see the mutation it was written for.
            for body in bodies:
                assert body["error"]["message"] == expected


# --------------------------------------------------------------------------
# A self-contained client. The repo's tests never import one another (`tests`
# is not a package under CI), so this builds the minimum surface the envelope
# needs rather than reaching into another test module.
# --------------------------------------------------------------------------


class _Resolver:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Orders:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no == "O-1"


class _Runtime:
    """Just enough to reach the 404 path: the row is absent."""

    def get_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str) -> None:
        return None

    def list_standard_diagnoses(self, context: ScopeContext, *, limit: int) -> list[dict]:
        return []


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Resolver(),
        order_authorizer=_Orders(),
    )
    return TestClient(app)


def test_job_failure_branch_codes_come_out_of_real_responses(tmp_path: Path) -> None:
    """The job surfaces' branch codes come from responses too, not a list.

    The request-level check covers 404/401; the FAILED-job codes (`QA_FAILED`,
    `DIAGNOSIS_FAILED`) are what a client branches on to choose its retry copy,
    so they belong here for the same reason: a hardcoded list stays green when
    the server starts returning something else.
    """
    from aiops_diagnostics.gateway_api import _assistant_question_response, _standard_diagnosis_response

    qa = {
        "qa_id": "qa_1",
        "question": "q",
        "status": "failed",
        "error_code": None,  # absent -> the documented default
        "result": None,
    }
    assert _assistant_question_response(dict(qa), "en")["error"]["code"] == "QA_FAILED"

    diagnosis = {
        "diagnosis_id": "dx_1",
        "order_no": "O-1",
        "question": "q",
        "indicator_code": None,
        "language": "en",
        "status": "failed",
        "error_code": None,
        "result": None,
        "created_at": "t",
        "updated_at": "t",
        "completed_at": None,
    }
    assert _standard_diagnosis_response(dict(diagnosis), "en")["error"]["code"] == "DIAGNOSIS_FAILED"
