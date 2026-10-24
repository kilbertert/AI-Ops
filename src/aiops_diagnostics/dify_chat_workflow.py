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

* 服务 API（`/v1/workflows/run` + `/v1/workflows/tasks/{task_id}/stop`），凭据是**该 app** 的
  service key，从 0600 文件读取，不进日志、不进 prompt、不进 App 响应；
* **已发布的 workflow id**，钉在绑定里；执行记录在调用前后各核对一次实际运行身份；
* 取消按 `task_id` 走 `/v1/workflows/tasks/{task_id}/stop`；blocking 模式下 `task_id` 由外层给出。

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    request_json,
)
from aiops_diagnostics.gateway_config import canonical_gateway_url
from aiops_diagnostics.private_files import PrivatePathError, validate_private_file

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
    ) -> WorkflowRun:
        """跑一次已发布的 workflow，返回它的执行身份。

        `user` 是 Dify 自己的调用者标识（我们用它承载"这一次请求"，**不是**个人授权）：
        个人授权在数据集那一侧由一次性凭据表达，模型与 Dify 用户字段都不参与授权。
        """
        if WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None:
            raise DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow unavailable")
        payload = request_json(
            RequestSpec(
                url=f"{self.base_url}{WORKFLOWS_PATH}/run",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {workflow_key}",
                },
                body=json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "inputs": inputs,
                        "response_mode": "blocking",
                        "user": user,
                    }
                ).encode("utf-8"),
                timeout=self.timeout_seconds,
                max_read_bytes=1048576,
                follow_redirects=False,
            ),
            mapping=_mapping(),
            envelope=_parse_workflow_envelope,
        )
        if not isinstance(payload, dict):
            raise DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow produced no result")
        # 真 Dify 1.17.1 的 blocking 形状是**两层**：
        #   {"task_id": ..., "workflow_run_id": ..., "data": {"id":..., "status":..., "outputs":...}}
        # 运行身份在外层，运行结果在内层。两层的键不通用，必须分开取。
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow produced no result")
        # 只有真的跑完了才算跑完：`status` 必须是 succeeded，且必须是**这一次**的 id。
        if data.get("status") != "succeeded":
            raise DifyWorkflowError("DIFY_WORKFLOW_FAILED", "dify workflow did not succeed")
        run_id = payload.get("workflow_run_id") or data.get("id")
        task_id = payload.get("task_id")
        if not isinstance(run_id, str) or not run_id or not isinstance(task_id, str) or not task_id:
            raise DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow produced no run identity")
        outputs = data.get("outputs")
        if not isinstance(outputs, dict):
            raise DifyWorkflowError("DIFY_WORKFLOW_INVALID", "dify workflow produced no outputs")
        created_at = data.get("created_at")
        return WorkflowRun(
            workflow_run_id=run_id,
            task_id=task_id,
            workflow_id=str(data.get("workflow_id") or workflow_id),
            outputs=outputs,
            status=str(data["status"]),
            created_at=created_at if isinstance(created_at, int) else None,
        )

    def stop(self, *, workflow_key: str, task_id: str, user: str) -> bool:
        """按 task id 请 Dify 停止。失败**不**影响本地已落的取消终态。"""
        try:
            request_json(
                RequestSpec(
                    # 真路由是 `/workflows/tasks/<task_id>/stop`，不是 `/workflows/run/<id>/stop`。
                    url=f"{self.base_url}{WORKFLOWS_PATH}/tasks/{task_id}/stop",
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


def _parse_workflow_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    """Dify Service API 的信封原样返回。

    **不解包 `data`**：真 1.17.1 里运行身份（`task_id` / `workflow_run_id`）在外层，
    运行结果（`status` / `outputs` / `id`）在内层 `data`。解包一次就把 `task_id` 弄丢了。
    形状判定放在 `run()` 里做，那里两层的含义都清楚。
    """
    if isinstance(payload, dict):
        return payload
    raise mapping.invalid_envelope(HttpFailure("invalid_envelope", detail="dify rejected"))
