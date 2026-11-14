from types import SimpleNamespace

import pytest

from aiops_diagnostics.knowledge.kb_onboarding import KbAdminClient, derive_email
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant, namespace_for
from aiops_diagnostics.publication.agent_debug import KbServiceKnowledgeClient


def test_same_tenant_in_two_environments_never_shares_knowledge_identity():
    assert knowledge_tenant("same", "env-a") != knowledge_tenant("same", "env-b")
    assert knowledge_tenant("b:c", "a") != knowledge_tenant("c", "a-b")
    assert namespace_for(SimpleNamespace(environment="95", environment_registry_file="registry")) == "95"
    assert namespace_for(SimpleNamespace(environment="41")) == ""
    assert knowledge_tenant("same") == "same"
    with pytest.raises(ValueError):
        namespace_for(SimpleNamespace(environment="", environment_registry_file="registry"))


def test_read_rebind_preserves_environment_and_matches_admin_header():
    read = KbServiceClient("http://127.0.0.1:1", tenant_id="initial", environment="95").for_tenant("same")
    admin = KbAdminClient("http://127.0.0.1:1", environment="95")
    assert read.tenant_id == "same" and read.environment == "95"
    assert admin._headers("same")["tenant-id"] == knowledge_tenant(read.tenant_id, read.environment)
    assert derive_email(knowledge_tenant("same", "95")) != derive_email("same")


def test_real_http_reads_and_writes_share_scope_and_other_environment_is_separate():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("tenant-id"), None))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[]}' if self.path == "/kb/knowledge-bases" else b'{"data":{}}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            calls.append((self.path, self.headers.get("tenant-id"), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":{}}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        a = KbAdminClient(base, environment="env-a")
        a.register_tenant(
            tenant_id="same", email=derive_email(knowledge_tenant("same", "env-a")), password="synthetic"
        )
        a.verify_login(tenant_id="same", password="synthetic")
        a.rotate_credential(tenant_id="same", old_password="synthetic", new_password="synthetic-new")
        KbServiceClient(base, tenant_id="initial", environment="env-a").for_tenant(
            "same"
        ).list_knowledge_bases()
        KbServiceClient(base, tenant_id="same", environment="env-b").list_knowledge_bases()
        KbServiceClient(base, tenant_id="same").list_knowledge_bases()
        publisher = KbServiceKnowledgeClient(base, tenant_id="initial", environment="env-a").for_tenant(
            "same"
        )
        publisher.knowledge_base("fixture")
        publisher.documents("fixture")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert len({c[1] for c in calls[:4]}) == 1
    assert calls[0][2]["biz_tenant_id"] == calls[0][1]
    assert len({calls[0][1], calls[4][1], calls[5][1]}) == 3
    assert calls[5][1] == "same"
    assert calls[6][1] == calls[7][1] == calls[0][1]


@pytest.mark.parametrize("process_environment", [None, "env-b"])
def test_reconcile_checks_knowledge_in_configured_environment(tmp_path, monkeypatch, process_environment):
    import os

    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable
    from aiops_diagnostics.publication import agent_debug

    for key in os.environ:
        if key.startswith("AIOPS_"):
            monkeypatch.delenv(key)
    config = tmp_path / "server.env"
    config.write_text(
        "AIOPS_GATEWAY_ENVIRONMENT=env-a\nAIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE=/fixture/catalogue.json\n"
    )
    config.chmod(0o600)
    if process_environment:
        monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", process_environment)
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        '[[agents]]\ntenant_id="same"\nname="客服"\nagent_type="customer"\nprompt="当前租户知识"\n'
        'knowledge_base_ids=["legacy-only"]\nmodel="aiops-api"\n'
    )
    headers = []

    def read(spec, **_):
        headers.append(spec.headers["tenant-id"])
        if spec.headers["tenant-id"] != "same":
            raise KnowledgeSearchUnavailable("not owned by scoped tenant")
        return {"docs": []}

    monkeypatch.setattr(agent_debug, "request_json", read)
    result = CliRunner().invoke(
        admin_app,
        [
            "--config",
            str(config),
            "reconcile",
            str(manifest),
            "--db",
            str(tmp_path / "gateway.db"),
            "--kb-url",
            "http://127.0.0.1:1",
        ],
    )
    assert headers == [knowledge_tenant("same", process_environment or "env-a")]
    assert result.exit_code != 0
