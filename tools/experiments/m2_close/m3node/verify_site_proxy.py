"""真实 Java 工具 → 真实 Gateway HTTP → 合成 CPMS，缺依赖即失败，不以跳过代替验证。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
from execution.test_m2_close_contract import _scope  # noqa: E402
from operations.test_operator_cpms import (  # noqa: E402
    HEADERS,
    NODE_A_TOKEN,
    environment,
    scope,
    tenant_owner,
    terminal,
)

from aiops_diagnostics.execution import execution_manifest  # noqa: E402
from aiops_diagnostics.execution.agent_runs import AgentRunConfig  # noqa: E402
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore  # noqa: E402
from aiops_diagnostics.identity.scope_context import DataScope  # noqa: E402
from aiops_diagnostics.operator import operator_cpms  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    deps = Path(os.environ["M3NODE_DEPS"]).resolve()
    image = os.environ["M3NODE_JDK_IMAGE"]
    assert "@sha256:" in image and (deps / "lib").is_dir()
    classes = out / "classes"
    classes.mkdir(exist_ok=True)
    docker = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--cap-drop=ALL",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{ROOT}:/repo:ro",
        "-v",
        f"{deps}:/deps:ro",
        "-v",
        f"{classes}:/out",
    ]
    sources = [
        str(p.relative_to(ROOT)) for p in (ROOT / "services/execution-node/src/main/java").rglob("*.java")
    ]
    subprocess.run(
        [
            *docker,
            "--network=none",
            image,
            "javac",
            "-nowarn",
            "-proc:none",
            "-cp",
            "/deps/lib/*",
            "-d",
            "/out",
            *["/repo/" + p for p in sources],
            "/repo/tools/experiments/m2_close/m3node/SiteProxyAcceptance.java",
        ],
        check=True,
        timeout=120,
    )
    results = []
    calls = []

    def java(f, run_id, manifest, arguments, *, empty=False, refused=False):
        result = subprocess.run(
            [
                *docker,
                "--network=host",
                "-i",
                image,
                "java",
                "-Daiops.node.tools-file=/repo/src/aiops_diagnostics/operator/operator_tools.json",
                "-cp",
                "/out:/deps/lib/*",
                "com.trendpower.aiops.node.SiteProxyAcceptance",
            ],
            input=json.dumps(
                {
                    "gateway": f["gateway_base"],
                    "token": NODE_A_TOKEN,
                    "run": run_id,
                    "manifest": manifest,
                    "arguments": arguments,
                }
            ),
            text=True,
            capture_output=True,
            timeout=30,
        )
        calls.append(result)
        (out / f"java-{len(calls)}.log").write_text(result.stdout + result.stderr)
        if refused:
            assert result.returncode != 0 and not result.stdout, result.stdout
            return None
        assert result.returncode == 0, result.stderr
        evidence = json.loads(result.stdout)
        assert [row["id"] for row in evidence["data"]["records"]] == ([] if empty else ["SITE-1"])
        assert "tenantId" not in result.stdout and "PRIVATE" not in result.stdout
        return evidence

    with tempfile.TemporaryDirectory(prefix="site-proxy-") as temporary, MonkeyPatch.context() as patch:  # noqa: SIM117
        with environment(Path(temporary), patch) as f:
            arguments = {}

            def dispatch(_, config, run_id):
                publication = f["client"].post(
                    "/v1/internal/charging-status/execution-config/" + run_id,
                    headers={"Authorization": "Bearer " + NODE_A_TOKEN},
                )
                assert publication.status_code == 200, publication.text
                java(
                    f,
                    run_id,
                    publication.json()["sha256"],
                    arguments,
                    empty=label == "empty",
                    refused=label == "wrong-tenant",
                )
                if label == "wrong-tenant":
                    assert not f["ctx"].operator_cpms.tasks[run_id].delivered
                    return
                assert "cpms_site_search" in f["ctx"].operator_cpms.tasks[run_id].delivered
                answer = {
                    "kind": "site_search",
                    "text": "查询结果如下。",
                    "name": arguments.get("name", ""),
                    "address_terms": ["天河"] if arguments.get("address") else [],
                }
                response = f["client"].post(
                    f"/v1/internal/operator-runs/{run_id}/completions",
                    headers={
                        "Authorization": "Bearer " + NODE_A_TOKEN,
                        "X-AI-Ops-Exec-Config": publication.json()["sha256"],
                    },
                    json={"status": "completed", "answer": json.dumps(answer)},
                )
                assert response.status_code == 200, response.text

            patch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
            for label, caller, arguments in [  # noqa: B007 - dispatch closure uses this case
                ("scoped", scope(), {}),
                ("keyword", scope(), {"name": "天河"}),
                ("spacing", scope(), {"address": "天 河"}),
                ("tenant", tenant_owner(), {}),
                ("wrong-tenant", scope(), {}),
                (
                    "empty",
                    replace(scope(), data_scope=DataScope(type="organ", site_ids=()), scope_fingerprint=""),
                    {},
                ),
            ]:
                f["resolver"].current = caller
                f["state"]["site_tenant"] = "OTHER" if label == "wrong-tenant" else "TENANT-A"
                before = len(f["state"]["calls"])
                response = f["client"].post(
                    "/v1/assistant/questions", headers=HEADERS, json={"question": "查询天河站点"}
                )
                assert response.status_code == 202, response.text
                final = terminal(f, response.json()["qa_id"])
                assert calls, final.text
                assert final.json()["status"] == ("failed" if label == "wrong-tenant" else "completed"), (
                    final.text
                )
                if label == "empty":
                    assert len(f["state"]["calls"]) == before
                results.append({"entry": "operator", "case": label, "status": "passed"})
    with (
        tempfile.TemporaryDirectory(prefix="consumer-site-proxy-") as temporary,
        MonkeyPatch.context() as patch,
        environment(Path(temporary), patch) as f,
    ):
        config = AgentRunConfig.load(Path(f["target"].config_file))
        f["settings"].charging_status_base_url = f["target"].cpms_base_url
        route = ChatRouteBindingStore(f["db"]).publish(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario="consumer_own_order_status",
            engine="node",
            agent_id=f["route"].agent_id,
            agent_version_no=f["route"].agent_version_no,
            app_id=f["target"].app_id,
            node_config_file=f["target"].config_file,
            expected_revision=0,
        )
        # 此处只签发测试运行；授权、范围推导、代理及证据接收均使用真实服务。
        target_file = Path(f["settings"].chat_node_targets_file)
        targets = json.loads(target_file.read_text())
        targets["targets"].append({**targets["targets"][0], "app_id": config.snail_app_id})
        target_file.write_text(json.dumps(targets))
        manifest = f["route"].manifest_text
        for label, arguments in [
            ("scoped", {}),
            ("keyword", {"name": "天河"}),
            ("empty", {}),
            ("wrong-tenant", {}),
        ]:
            f["state"]["empty_orders"] = label == "empty"
            f["state"]["site_tenant"] = "OTHER" if label == "wrong-tenant" else "TENANT-A"
            caller = _scope()
            run = f["ctx"].agent_run_store.issue(
                scope=caller,
                order_no="",
                config=config,
                gateway_version="test",
                expected_agent_version=execution_manifest.from_text(manifest).identity,
                execution_manifest=manifest,
                chat_binding=route.key,
                route_revision=route.revision,
            )
            qa = f["ctx"].store.create_assistant_question(caller.scope_fingerprint, "查询天河站点")
            assert f["ctx"].agent_run_store.bind_qa(run.run_id, qa["qa_id"])
            before = len(f["state"]["calls"])
            java(
                f,
                run.run_id,
                run.expected_agent_version,
                arguments,
                empty=label == "empty",
                refused=label == "wrong-tenant",
            )
            evidence = f["ctx"].consumer_tool_grants.grants[run.run_id].evidence
            assert (evidence is None) == (label == "wrong-tenant")
            if label == "empty":
                assert all("getOrderUserPage" in call[0] for call in f["state"]["calls"][before:])
            results.append({"entry": "consumer", "case": label, "status": "passed"})
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
