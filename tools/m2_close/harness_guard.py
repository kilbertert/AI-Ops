#!/usr/bin/env python3
"""隔离实验的两件公共设施：**跨工作树的互斥锁**与**代码身份**。

两者都存在的原因都是同一条实测：实验的容器、网络与端口名字是固定的，
两份实验同时跑会互相删除对方的容器；而报告里的身份如果是事后手工补的，
它就不再是「这次运行实际跑了什么」。

互斥锁
------
固定路径上的一个 ``flock``，**不区分工作树**：谁先拿到谁跑，第二个立刻报
``experiment busy`` 并退出，**在任何 docker 调用与清理动作之前**。正常退出、
异常退出（包括 Ctrl-C）都由进程退出释放。同目录与跨工作树都共用这把锁；
固定容器名不能通过环境变量换一把锁绕开互斥。
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
from typing import IO

#: 固定路径，跨工作树、跨仓库副本共用同一把锁。
LOCK_PATH = Path("/tmp/aiops-m2close-experiment.lock")

#: 参与身份计算的**源文件**（相对仓库根）。节点 jar 单独用摘要，因为它由构建产生。
IDENTITY_FILES = (
    "src/aiops_diagnostics/gateway_api.py",
    "src/aiops_diagnostics/execution/agent_runs.py",
    "src/aiops_diagnostics/gateway_config.py",
    "src/aiops_diagnostics/data/charging_status.py",
    "tools/m2_close/harness_guard.py",
    "tools/m2_close/run_ic.py",
    "tools/m2_close/synthetic_upstream.py",
    "tools/m2_close/gateway_launcher.py",
    "tools/m2_close/verify_real_gateway.py",
    "services/execution-node/pom.xml",
    "services/execution-node/src/main/resources/application.yml",
    "services/execution-node/src/main/java/com/trendpower/aiops/node/GovernedToolRuntime.java",
    "services/execution-node/src/main/java/com/trendpower/aiops/node/ControlledChargingStatusTool.java",
    "services/execution-node/src/main/java/com/trendpower/aiops/node/ExecutionNodeApplication.java",
)


class ExperimentBusy(RuntimeError):
    """另一个实验正持有锁。调用方应当**立即**退出，不做任何清理。"""


def acquire(path: Path | None = None) -> IO[str]:
    """非阻塞地拿锁；拿不到就抛 :class:`ExperimentBusy`。返回的句柄必须活到实验结束。"""
    target = path or LOCK_PATH
    handle = open(target, "a+", encoding="utf-8")  # noqa: SIM115 - 句柄的生命周期就是这么用的
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ExperimentBusy(str(target)) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def code_identity(repo: Path, *, extra: dict[str, str] | None = None) -> dict[str, object]:
    """记录本次验证范围内的源文件与构建产物摘要；必需源文件缺失直接失败。"""
    files: dict[str, str] = {}
    for name in IDENTITY_FILES:
        path = repo / name
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity: dict[str, object] = {"files": files}
    if extra:
        identity.update(extra)
    return identity


def identity_drift(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """两次采样的差异；非空即「运行期间代码变了」，报告不得算作通过。"""
    changed: list[str] = []
    for name, digest in dict(before.get("files", {})).items():
        if dict(after.get("files", {})).get(name) != digest:
            changed.append(name)
    for key in ("node_jar_sha256", "platform_commit", "platform_jar_sha256"):
        if key in before and before.get(key) != after.get(key):
            changed.append(key)
    return changed
