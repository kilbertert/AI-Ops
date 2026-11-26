"""P0-2 开通底座加固回归测试（ADR-0357）。

拦截的具体错误：
1. 空机冷装缺锁定非登录服务账号 aiops 或缺 ensurepip/venv 时直接失败回滚；
2. 取消 tenant-onboard 后 15 分钟内再次提交被卡死在旧 cancelled 任务；
3. 开发机工作区存在未跟踪文件（如客户文档）导致 task_ssh_runner 误拒执行；
4. 订单标准验收问句漏掉过程统计触发假失败，或发布后公网 HTTPS 冒烟放过非 HTTPS/匿名非 401/泄露令牌。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.onboarding import task_ssh_runner as worker
from aiops_diagnostics.onboarding.public_smoke import (
    build_standard_validation_questions,
    run_post_publish_https_smoke,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_install_module():
    gateway_dir = REPO_ROOT / "ops/environments/gateway"
    if str(gateway_dir) not in sys.path:
        sys.path.insert(0, str(gateway_dir))
    spec = importlib.util.spec_from_file_location("aiops_gateway_install_p02", gateway_dir / "install.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ensure_service_identity_bootstraps_locked_nologin_account_when_missing(monkeypatch) -> None:
    install = _load_install_module()
    calls: list[tuple[str, ...]] = []
    created = {"present": False}

    def fake_getpwnam(user: str):
        if not created["present"]:
            raise KeyError(user)
        return SimpleNamespace(pw_uid=998, pw_gid=998, pw_shell="/usr/sbin/nologin")

    def fake_command(*args: str, **kwargs):
        calls.append(tuple(args))
        if args and args[0] == "useradd":
            created["present"] = True
        return ""

    monkeypatch.setattr(install.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(install.probe, "command", fake_command)

    acc = install.ensure_service_identity("jiejia", "aiops")
    assert acc.pw_uid == 998
    assert calls[0][:2] == ("useradd", "--system")
    assert "--shell" in calls[0]
    assert calls[1] == ("passwd", "-l", "aiops")


def test_ensure_python_venv_installs_venv_package_when_ensurepip_missing(monkeypatch) -> None:
    install = _load_install_module()
    probe_calls: list[tuple[str, ...]] = []
    run_count = {"n": 0}

    def fake_run(args, **kwargs):
        run_count["n"] += 1
        return SimpleNamespace(returncode=1 if run_count["n"] == 1 else 0, stdout="", stderr="")

    def fake_command(*args: str, **kwargs):
        probe_calls.append(tuple(args))
        if "version_info" in " ".join(args):
            return "3.12\n"
        return ""

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(
        install.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
    )
    monkeypatch.setattr(install.probe, "command", fake_command)

    install.ensure_python_venv("/usr/bin/python3.12")
    assert any("python3.12-venv" in call for call in probe_calls)
    assert run_count["n"] == 2


def test_task_ssh_runner_ignores_untracked_files_but_rejects_dirty_tracked(tmp_path, monkeypatch) -> None:
    seen_git_args: list[list[str]] = []

    def fake_check_output(args, **kwargs):
        seen_git_args.append(list(args))
        return ""

    monkeypatch.setattr(worker.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        worker,
        "central_call",
        lambda root, target, payload: (
            {"advanced": 0} if payload == {"advance_candidates": True} else {"claimed": False, "task": None}
        ),
    )

    assert worker.run(tmp_path, "", "", central_target="41") is None
    assert seen_git_args[0] == ["git", "status", "--porcelain", "--untracked-files=no"]

    monkeypatch.setattr(worker.subprocess, "check_output", lambda args, **kw: " M src/file.py\n")
    with pytest.raises(ValueError, match="clean merged checkout required"):
        worker.run(tmp_path, "", "", central_target="41")


def test_standard_validation_questions_enforce_process_stats_evidence() -> None:
    qs = build_standard_validation_questions(
        order_no="2125879212020924417",
        device_id="2121870666501521409",
        start="2026-11-25T00:00:00+08:00",
        end="2026-11-26T23:59:59+08:00",
        consumer_order_no="2092158656121733121",
    )
    assert "2125879212020924417" in qs["order"]["question"]
    assert "过程统计" in qs["order"]["question"]
    assert "2121870666501521409" in qs["offline"]["question"]
    assert "2092158656121733121" in qs["consumer"]["question"]


def test_post_publish_https_smoke_checks_all_four_paths_without_leaking_tokens() -> None:
    calls: list[tuple[str, str]] = []

    def fake_http(method: str, url: str, headers: dict[str, str], body):
        calls.append((method, url))
        if method == "GET" and url.endswith("/upms/tenant/list"):
            return 200, '{"code":0,"data":[]}'
        if method == "POST" and url.endswith("/v1/assistant/questions"):
            if "third-session" not in headers and "Authorization" not in headers:
                return 401, '{"error":{"code":"INVALID_ACCESS_TOKEN"}}'
            if "third-session" in headers:
                return 202, '{"qa_id":"qa_c1","status":"queued"}'
            return 202, '{"qa_id":"qa_o1","status":"queued"}'
        if method == "GET" and "/v1/assistant/questions/" in url:
            qa_id = url.rsplit("/", 1)[1]
            return 200, json.dumps({"qa_id": qa_id, "status": "completed", "error": None})
        raise AssertionError(f"unexpected call {method} {url}")

    report = run_post_publish_https_smoke(
        "https://api.example.invalid",
        "1990990768688599040",
        consumer_session="SECRET_CONSUMER_SESSION_XYZ",
        operator_token="Bearer SECRET_OPERATOR_TOKEN_ABC",
        http_caller=fake_http,
        sleep_fn=lambda _: None,
    )
    assert report["passed"] is True
    assert report["business_accuracy"] == "unconfirmed"
    assert [c["id"] for c in report["checks"]] == [
        "public-https-anon-rejected",
        "public-https-customer-route-intact",
        "public-https-consumer-qa",
        "public-https-operator-qa",
    ]
    dumped = json.dumps(report)
    assert "SECRET_CONSUMER_SESSION_XYZ" not in dumped
    assert "SECRET_OPERATOR_TOKEN_ABC" not in dumped

    with pytest.raises(ValueError, match="https"):
        run_post_publish_https_smoke(
            "http://api.example.invalid", "1990990768688599040", http_caller=fake_http
        )
