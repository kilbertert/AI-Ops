"""管家提示词换版不中断已签发任务，撤权仍立即生效。"""

import pytest

from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import HEADERS, environment, node, request, terminal


@pytest.mark.parametrize("stage", ["before_tools", "before_complete", "completed"])
def test_operator_prompt_publish_preserves_old_run(tmp_path, monkeypatch, stage):
    with environment(tmp_path, monkeypatch) as f:

        def publish_next():
            f["candidate"]["prompt"] = "SECOND_STYLE"
            f["publish"](f["route"].route_revision)

        def dispatch(_, cfg, run):
            if stage == "before_tools":
                publish_next()
            response = node(f, run, before_complete=publish_next if stage == "before_complete" else None)
            assert response.status_code == 200, response.text

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        started = request(f["client"])
        assert started.status_code == 202, started.text
        qa = started.json()["qa_id"]
        final = terminal(f, qa)
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text
        if stage == "completed":
            publish_next()
        readback = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
        assert readback.status_code == 200 and readback.json()["result"] == final.json()["result"]
        from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

        routes = ChatRouteBindingStore(f["db"])
        current = routes.get("env-a", "TENANT-A", "operator", operator_cpms.SCENARIO)
        routes.retract(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="operator",
            scenario=operator_cpms.SCENARIO,
            expected_revision=current.revision,
        )
        assert f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS).status_code == 410
