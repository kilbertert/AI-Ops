"""R1-3：IC 聚合门必须**可靠失败**。

审查的最小复现是：只把 IC-07 记成 ``UNEXPECTED``、其余编号全缺，旧实现仍输出
PASS 且退出码 0。这里把那条复现钉成回归 —— 任何「缺项/UNEXPECTED/非 PASS 被写成 PASS」
的回归都会让本文件转红。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tools" / "experiments" / "m2_close" / "run_ic.py"


def _module():
    spec = importlib.util.spec_from_file_location("m2_close_run_ic", HARNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _all_pass() -> dict[str, str]:
    return {name: f"PASS：合成断言 {name}" for name in _module().REQUIRED_CHECKS}


def test_the_reviewer_minimal_reproduction_is_fail():
    """审查的最小复现：只有一条 UNEXPECTED、缺其余编号 ⇒ FAIL，不是 PASS。"""
    status, reasons = _module().aggregate({"IC-07": "UNEXPECTED：对抗模式仍返回 200"})
    assert status == "FAIL"
    assert any("缺少编号" in reason for reason in reasons)
    assert any("UNEXPECTED" in reason for reason in reasons)


def test_missing_checks_fail_even_when_every_present_check_passes():
    checks = _all_pass()
    del checks["IC-06"]
    status, reasons = _module().aggregate(checks)
    assert status == "FAIL" and any("IC-06" in reason for reason in reasons)


def test_empty_checks_fail():
    assert _module().aggregate({})[0] == "FAIL"


def test_one_non_pass_check_fails():
    checks = _all_pass()
    checks["IC-04"] = "未完成：领取归属未核对"
    assert _module().aggregate(checks)[0] == "FAIL"


def test_only_a_complete_all_pass_set_is_pass():
    status, reasons = _module().aggregate(_all_pass())
    assert status == "PASS" and reasons == []


def test_unexpected_anywhere_fails_even_if_prefixed_with_pass():
    """``PASS ... UNEXPECTED`` 这种自相矛盾的串同样算失败。"""
    checks = _all_pass()
    checks["IC-07"] = "PASS：但 UNEXPECTED：对抗模式仍返回 200"
    assert _module().aggregate(checks)[0] == "FAIL"


def test_an_execution_error_fails_even_when_all_ten_checks_passed():
    """R2-2 的复现形态：十项都写成 PASS 之后又抛了异常，总判定仍必须是 FAIL。

    这是**真实**发生过的形态：IC-10 已赋值、最后 IC-06b 的断言失败。旧实现只按
    checks 聚合，于是「十项齐全 + 全 PASS」让总报告写成 PASS、退出码 0，而 error 字段
    同时写着 AssertionError —— 一份自相矛盾的报告。
    """
    checks = _all_pass()
    status, reasons = _module().aggregate(checks, error="AssertionError: IC-06b failed")
    assert status == "FAIL"
    assert any("执行期间发生异常" in reason for reason in reasons)


def test_error_with_partial_checks_also_fails():
    status, reasons = _module().aggregate({"IC-01": "PASS：x"}, error="boom")
    assert status == "FAIL"
    assert any("执行期间发生异常" in reason for reason in reasons)
    assert any("缺少编号" in reason for reason in reasons)


def test_cli_aggregate_only_fails_a_complete_report_that_carries_an_error(tmp_path):
    """真实 CLI 层：十项齐全 + 全 PASS + error 字段 ⇒ 退出码 1、status FAIL。"""
    report = tmp_path / "complete-with-error.json"
    report.write_text(json.dumps({"checks": _all_pass(), "error": "AssertionError: late failure"}))
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--aggregate-only", "--out", str(report)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    written = json.loads(report.read_text())
    assert written["status"] == "FAIL"
    assert written["status_reasons"]


def test_cli_aggregate_only_passes_only_the_complete_all_pass_report(tmp_path):
    report = tmp_path / "complete.json"
    report.write_text(json.dumps({"checks": _all_pass()}))
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--aggregate-only", "--out", str(report)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(report.read_text())["status"] == "PASS"


def test_harness_exits_non_zero_for_a_partial_report(tmp_path):
    """用一次**真实**的 CLI 调用证明聚合门接在退出码上，而不只是函数返回值。"""
    report = tmp_path / "partial.json"
    report.write_text(json.dumps({"checks": {"IC-07": "UNEXPECTED：x"}}))
    # 直接驱动 main 的聚合段：--aggregate-only 让脚本不碰容器。
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--aggregate-only", "--out", str(report)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    written = json.loads(report.read_text())
    assert written["status"] == "FAIL"
    assert any("UNEXPECTED" in reason for reason in written["status_reasons"])


# --------------------------------------------------------------------------
# 互斥锁：第二个实验必须在任何 Docker/清理动作之前退出
# --------------------------------------------------------------------------


def test_second_experiment_exits_busy_before_touching_docker(tmp_path, monkeypatch):
    """两个进程：第一个持有锁期间，第二个必须立即退出，且**一次 docker 调用都不发**。

    这条不是测 fcntl —— 它测的是「第二个进程在任何清理之前就退出」这个顺序。没有它，
    第二次运行会先 `docker rm -f` 掉第一轮的容器，再报一个看起来像环境问题的错。
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    lock_path = tmp_path / "experiment.lock"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                f"h = open({str(lock_path)!r}, 'a+')\n"
                "fcntl.flock(h.fileno(), fcntl.LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "time.sleep(30)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        # 用一个会记录调用的假 docker：脚本若去调它，就会留下痕迹。
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "docker-was-called"
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(f'#!/bin/sh\necho "$@" >> {marker}\nexit 0\n')
        fake_docker.chmod(0o755)

        env = {
            **__import__("os").environ,
            "NODE_JAR": str(tmp_path / "missing.jar"),
            "SNAIL_SRC": str(tmp_path / "missing-platform"),
            "PATH": f"{bin_dir}:{__import__('os').environ['PATH']}",
        }
        # 只走到拿锁那一步：给一个不存在的产物路径，若锁没拦住就会落到「环境不足」分支。
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,runpy; from pathlib import Path; "
                f"sys.path.insert(0, {str(repo / 'tools/experiments/m2_close')!r}); "
                "import harness_guard; "
                f"harness_guard.LOCK_PATH=Path({str(lock_path)!r}); "
                f"runpy.run_path({str(repo / 'tools/experiments/m2_close/run_ic.py')!r}, "
                "run_name='__main__')",
                "--out",
                str(tmp_path / "out.json"),
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 3, completed.stderr
        assert "experiment busy" in completed.stderr
        assert not marker.exists(), f"第二个进程在退出前调用过 docker：{marker.read_text()}"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_experiment_lock_is_released_after_the_process_exits(tmp_path):
    """锁由进程退出释放：前一个进程退出后，同一路径必须能再次拿到。"""
    import subprocess
    import sys

    lock_path = tmp_path / "experiment.lock"
    script = (
        "import fcntl\n"
        f"h = open({str(lock_path)!r}, 'a+')\n"
        "fcntl.flock(h.fileno(), fcntl.LOCK_EX)\n"
        "print('ok')\n"
    )
    first = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert first.returncode == 0 and first.stdout.strip() == "ok"
    second = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert second.returncode == 0 and second.stdout.strip() == "ok"


def test_real_experiment_lock_cannot_be_split_by_environment(tmp_path):
    import os

    code = (
        f"import sys; sys.path.insert(0, {str(REPO / 'tools/experiments/m2_close')!r}); "
        "import harness_guard; print(harness_guard.LOCK_PATH)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "M2CLOSE_LOCK": str(tmp_path / "other.lock")},
        check=True,
    )
    assert result.stdout.strip() == "/tmp/aiops-m2close-experiment.lock"


def test_missing_source_file_cannot_be_recorded_as_valid_identity(tmp_path):
    import pytest

    module = _module()
    with pytest.raises(FileNotFoundError):
        module.harness_guard.code_identity(tmp_path)


def test_platform_identity_is_resampled_after_the_experiment(tmp_path, monkeypatch):
    import argparse
    from types import SimpleNamespace

    module = _module()
    platform = tmp_path / "platform"
    artifact = platform / "snail-ai-starter/target/snail-ai-server-exec.jar"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"platform-before")
    node = tmp_path / "node.jar"
    node.write_bytes(b"node")
    monkeypatch.setenv("SNAIL_SRC", str(platform))
    monkeypatch.setenv("NODE_JAR", str(node))
    revisions = iter(["first", "second"])
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=next(revisions)))
    monkeypatch.setattr(module, "Stack", lambda *a: SimpleNamespace(start=lambda: None, stop=lambda: None))

    def prove(stack, checks):
        checks.update({f"IC-{i:02}": "PASS: synthetic" for i in range(1, 11)})
        artifact.write_bytes(b"platform-after")
        return {}

    monkeypatch.setattr(module, "prove", prove)
    out = tmp_path / "report.json"
    assert module._run(argparse.Namespace(out=out, keep=False), None) == 1
    report = json.loads(out.read_text())
    assert report["status"] == "FAIL"
    assert set(report["identity_drift"]) == {"platform_commit", "platform_jar_sha256"}
