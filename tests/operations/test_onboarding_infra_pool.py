"""离线制品池与外围基建自动编排测试（ADR-0358 P1-3）。

拦截的具体错误：
1. 制品池返回篡改后的归档或向前端泄露服务端绝对文件路径；
2. 非法 IP/域名/非 HTTPS 网关地址进入白名单、节点或 Nginx 模板；
3. Nginx 渲染遗漏方法白名单 405 或探活匿名非 401/原路由非 200 时误判通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.onboarding.artifact_pool import (
    ArtifactPoolRefused,
    list_pool_status,
    register_pool_entry,
    resolve_artifact_for_python,
)
from aiops_diagnostics.onboarding.infra_orchestrator import (
    InfraProvisionRefused,
    plan_egress_allowlist,
    plan_execution_node,
    provision_environment_infra,
    render_customer_nginx_extensions,
)


def test_artifact_pool_registers_resolves_and_detects_tampering(tmp_path: Path) -> None:
    pool_file = tmp_path / "pool" / "artifact-pool.json"
    app_tar = tmp_path / "pool" / "app-py312.tar.gz"
    inst_tar = tmp_path / "pool" / "installer.tar.gz"
    app_tar.parent.mkdir(mode=0o700)
    write_private_text(app_tar, "mock-app-archive")
    write_private_text(inst_tar, "mock-installer-archive")

    rev = "e" * 40
    meta = register_pool_entry(
        pool_file,
        revision=rev,
        python_minor="3.12",
        app_archive=app_tar,
        installer_archive=inst_tar,
        actor="ops",
    )
    assert meta["revision"] == rev
    assert meta["python_minor"] == "3.12"

    resolved = resolve_artifact_for_python(pool_file, "3.12")
    assert resolved["revision"] == rev
    assert resolved["application"]["sha256"] == meta["application_sha256"]

    # 脱敏只读列表绝不含服务端绝对文件路径
    status = list_pool_status(pool_file)
    assert status["count"] == 1
    assert str(tmp_path) not in json.dumps(status)

    # 磁盘文件被篡改必须 fail-closed 拒绝
    write_private_text(app_tar, "tampered-content")
    with pytest.raises(ArtifactPoolRefused, match="DIGEST_DRIFT"):
        resolve_artifact_for_python(pool_file, "3.12")


def test_infra_orchestrator_enforces_validation_and_live_probe_contract() -> None:
    with pytest.raises(InfraProvisionRefused, match="INVALID_IP"):
        plan_egress_allowlist("jiejia", "not-an-ip")
    with pytest.raises(InfraProvisionRefused, match="HTTPS_REQUIRED"):
        plan_execution_node("jiejia", gateway_url="http://api.juxichongdian.cn")

    nginx = render_customer_nginx_extensions(
        "jiejia",
        api_domain="api.juxichongdian.cn",
        allow_sources=["47.110.1.1"],
    )
    assert "return 405;" in nginx["files"]["aiops-user-ingress.conf"]
    assert "allow 47.110.1.1;" in nginx["files"]["aiops-tool-ingress.conf"]
    assert nginx["auth_snippet_mode"] == "0600"

    # 全绿路径
    ok_res = provision_environment_infra(
        "jiejia",
        egress_ip="8.148.223.139",
        api_domain="api.juxichongdian.cn",
        gateway_url="https://api.juxichongdian.cn",
        allow_sources=["47.110.1.1"],
        allowlist_runner=lambda p: {"ok": True},
        node_runner=lambda p: {"ok": True},
        nginx_runner=lambda p: {"ok": True},
        live_probe=lambda url: {"anon_assistant_status": 401, "upms_tenant_list_status": 200},
    )
    assert ok_res["passed"] is True
    assert [s["step"] for s in ok_res["steps"]] == [
        "allowlist",
        "execution_node",
        "customer_nginx",
        "live_probe",
    ]

    # Nginx 失败立即阻断且不继续探活
    probe_called = {"n": 0}
    ng_fail = provision_environment_infra(
        "jiejia",
        egress_ip="8.148.223.139",
        api_domain="api.juxichongdian.cn",
        gateway_url="https://api.juxichongdian.cn",
        allowlist_runner=lambda p: {"ok": True},
        node_runner=lambda p: {"ok": True},
        nginx_runner=lambda p: {"ok": False, "rolled_back": True, "reason": "NGINX_TEST_FAILED"},
        live_probe=lambda url: probe_called.__setitem__("n", 1) or {},
    )
    assert ng_fail["passed"] is False
    assert ng_fail["failed_step"] == "customer_nginx"
    assert probe_called["n"] == 0

    # 匿名非 401 探活失败阻断
    probe_fail = provision_environment_infra(
        "jiejia",
        egress_ip="8.148.223.139",
        api_domain="api.juxichongdian.cn",
        gateway_url="https://api.juxichongdian.cn",
        allowlist_runner=lambda p: {"ok": True},
        node_runner=lambda p: {"ok": True},
        nginx_runner=lambda p: {"ok": True},
        live_probe=lambda url: {"anon_assistant_status": 200, "upms_tenant_list_status": 200},
    )
    assert probe_fail["passed"] is False
    assert probe_fail["failed_step"] == "live_probe"
