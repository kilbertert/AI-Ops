"""把一次「本人订单状态」交给**已发布的 Dify Workflow** 跑（APP-RUNTIME-02）。

## 与节点路线的关系

节点路线（`chat_execution` + `agent_runs`）已经把「谁授权、选哪一单、结果怎么放行」做完了。
本模块**不重做**那些判据，它只负责一件事：把这次 QA 交给 Dify 的 Service API，并留下
**可核对的执行身份**。

两条路线在绑定表里用 `engine` 明确区分，一条登记只指向一个引擎。没有"节点失败就试 Dify"：
ADR-0079 的判据是**一次请求只有一个模型循环、无故障自动跨路线回落**。

## 为什么用 Service API 而不是 console 预览 API

console 的预览接口认的是**编辑者会话**，跑的是**草稿**。用它意味着"用户看到的那次回答
来自一份还没发布的图"，而发布与撤回不可核对。这里固定：

* 服务 API（`/v1/workflows/{workflow_id}/run` + `/v1/workflows/tasks/{task_id}/stop`），凭据是**该 app** 的
  service key，从 0600 文件读取，不进日志、不进 prompt、不进 App 响应；
* **已发布的 workflow id**，钉在绑定里；执行记录在调用前后各核对一次实际运行身份；
* 取消按 `task_id` 走 `/v1/workflows/tasks/{task_id}/stop`；开始事件到达即持久化 `task_id`，不等最终结果。

## 凭据（取数授权）的边界

Dify 侧需要拿到"这一单、这一次"的状态数据。它可以调到 Gateway 的一个受限端点，凭据是
`chat_data_grant` 签发的一次性 token：

* ≥256 bit 随机，库里只存哈希；
* 绑主体指纹、订单、环境、租户、QA、workflow 身份、有效期（沿用 180 秒）；
* `run_id` 或 `qa_id` 单独出现**不能**读数据；
* 每次回调都重做当前核权、有效期、取消、绑定检查；只允许这一张单，首次读一次业务。

**已知且写明的限制**：Dify 的 inputs 与运行记录会保存这次 input，因此短期内
该 token 对**能打开这个 Dify 应用记录的人**可见。这一点不是"已消除"，是"被接受并在
文档里写明"；完成/失败/取消后立即撤销，不声称能追回此前已披露的数据。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.bounded_http import (
    TRANSPORT_FAILURES,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    open_response,
    quote_segment,
    request_json,
)
from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file
from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

#: Dify workflow id 的形状（1.17.x 的 UUID）。不匹配就拒绝，不做兼容读取。
WORKFLOW_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Service API 的固定前缀。1.17.x 的 workflow 运行接口在 `/v1/workflows/...`。
WORKFLOWS_PATH = "/v1/workflows"

#: 一次 workflow 运行的最长等待。网关的 run TTL 是 180 秒，这里必须小于它 ——
#: 否则"还在等 Dify"的时候凭据已经过期，回调会被自己的有效期判据挡住。
DEFAULT_RUN_TIMEOUT_SECONDS = 120


class DifyWorkflowError(RuntimeError):
    """Dify 调用失败。``code`` 是回给调用方的稳定错误码，**不**转述上游正文。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def load_workflow_key(path: str | Path | None) -> str:
    """读该 app 的 service key。文件必须私有（0600），内容只有一行 token。"""
    if not path:
        raise ValueError("dify workflow key is not configured")
    declared = Path(str(path)).expanduser().absolute()
    try:
        validate_private_file(declared)
        raw = declared.read_text(encoding="utf-8").strip()
    except (OSError, PrivatePathError) as exc:
        raise ValueError("dify workflow key is unavailable") from exc
    if not raw or len(raw) > 512 or any(ch.isspace() for ch in raw):
        # 一个带空白的凭据多半是把说明文字也粘进去了；拒绝比发出去更好。
        raise ValueError("dify workflow key is malformed")
    return raw


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """一次**已经发生**的 workflow 运行的执行身份。

    ``workflow_run_id`` 与 ``task_id`` 都来自 Dify 自己的响应，不是我们拼的 ——
    取消要按它去停，取证要按它去对。
    """

    workflow_run_id: str
    task_id: str
    workflow_id: str
    outputs: dict[str, Any]
    status: str
    created_at: int | None


def _failure(_failure: HttpFailure) -> DifyWorkflowError:
    # 上游正文可能带内部地址或凭据片段，不向调用方转述。
    return DifyWorkflowError("DIFY_UNAVAILABLE", "dify workflow unavailable", retryable=True)


def _mapping() -> ErrorMapping:
    return ErrorMapping(
        auth_rejected=_failure,
        http_error=_failure,
        unavailable=_failure,
        invalid_body=_failure,
        invalid_envelope=_failure,
    )


class DifyWorkflowClient:
    """Dify Service API 的最小客户端：跑一次、停一次。没有第三步。"""

    def __init__(self, base_url: str, *, timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS) -> None:
        self.base_url = canonical_gateway_url(base_url)
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        workflow_key: str,
        workflow_id: str,
        inputs: dict[str, Any],
        user: str,
        on_started: Callable[[str, str], None],
    ) -> WorkflowRun:
        """执行指定发布版；收到开始事件即交出真实身份，最终事件才交出答案。"""
        if WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None:
            raise _invalid()
        spec = RequestSpec(
            url=f"{self.base_url}{WORKFLOWS_PATH}/{workflow_id}/run",
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {workflow_key}"},
            body=json.dumps({"inputs": inputs, "response_mode": "streaming", "user": user}).encode(),
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )
        identity: tuple[str, str] | None = None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with open_response(spec) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise _invalid()
                for event in _events(response, deadline):
                    kind = event.get("event")
                    if kind == "error":
                        raise DifyWorkflowError("DIFY_WORKFLOW_FAILED", "dify workflow did not succeed")
                    if kind not in {"workflow_started", "workflow_finished"}:
                        if identity is not None:
                            for key, expected in zip(("workflow_run_id", "task_id"), identity, strict=True):
                                if key in event and event[key] != expected:
                                    raise _invalid()
                        continue
                    data = event.get("data")
                    if not isinstance(data, dict):
                        raise _invalid()
                    actual = (event.get("workflow_run_id"), event.get("task_id"))
                    if not all(isinstance(value, str) and value.strip() for value in actual):
                        raise _invalid()
                    if data.get("id") != actual[0] or not isinstance(data.get("workflow_id"), str):
                        raise _invalid()
                    if data["workflow_id"] != workflow_id:
                        raise DifyWorkflowError("DIFY_WORKFLOW_MISMATCH", "dify ran a different workflow")
                    if kind == "workflow_started":
                        if identity is not None:
                            raise _invalid()
                        identity = actual
                        on_started(*identity)
                        continue
                    if identity is None or actual != identity:
                        raise _invalid()
                    if data.get("status") != "succeeded":
                        raise DifyWorkflowError("DIFY_WORKFLOW_FAILED", "dify workflow did not succeed")
                    outputs = data.get("outputs")
                    if not isinstance(outputs, dict):
                        raise _invalid()
                    created_at = data.get("created_at")
                    return WorkflowRun(
                        workflow_run_id=identity[0],
                        task_id=identity[1],
                        workflow_id=data["workflow_id"],
                        outputs=outputs,
                        status="succeeded",
                        created_at=created_at if isinstance(created_at, int) else None,
                    )
        except TRANSPORT_FAILURES as exc:
            raise _failure(HttpFailure("unavailable")) from exc
        # EOF、断流或只有开始事件，都不能冒充完成。
        raise _invalid()

    def stop(self, *, workflow_key: str, task_id: str, user: str) -> bool:
        """按 task id 请 Dify 停止。失败**不**影响本地已落的取消终态。"""
        try:
            request_json(
                RequestSpec(
                    # 真路由是 `/workflows/tasks/<task_id>/stop`，不是 `/workflows/run/<id>/stop`。
                    url=f"{self.base_url}{WORKFLOWS_PATH}/tasks/{quote_segment(task_id)}/stop",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {workflow_key}",
                    },
                    body=json.dumps({"user": user}).encode("utf-8"),
                    timeout=10,
                    max_read_bytes=65536,
                    follow_redirects=False,
                ),
                mapping=_mapping(),
                envelope=lambda payload, mapping: payload,
            )
        except DifyWorkflowError:
            # 供应商没停 ≠ 我们没取消：本地终态与凭据撤销已经落库，这里只如实返回 False。
            return False
        return True


def _invalid() -> DifyWorkflowError:
    return DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow produced invalid execution evidence")


def _events(response: Any, deadline: float) -> Iterator[dict[str, Any]]:
    """读取 Dify 的 SSE data 帧；累计字节和整个调用时间都有上限，不重连。"""
    pending = b""
    data: list[bytes] = []
    total = 0
    transport = response.fp.raw._sock
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        # urllib 的 HTTPResponse 保留同一 socket；每次 recv 用剩余预算，防止慢流无限续时。
        if response.isclosed():
            return
        transport.settimeout(remaining)
        chunk = response.read1(8192)
        if not chunk:
            return
        total += len(chunk)
        if total > 1048576:
            raise _invalid()
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if not line:
                if data:
                    payload = json.loads(b"\n".join(data).decode("utf-8"))
                    data = []
                    if not isinstance(payload, dict):
                        raise _invalid()
                    yield payload
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
