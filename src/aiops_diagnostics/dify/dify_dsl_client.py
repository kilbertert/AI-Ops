"""Dify DSL 导出与控制台只读元数据 HTTP 客户端。

负责 ``DifyDslSource`` 配置、异常体系（``DifyDslError*``）、``DifyDslClient`` 重试导出、
数据集/工具提供者读取及响应信封解包；作为 ``dify_dsl_*`` 无环链最底层，不依赖映射或编排。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    RetryPolicy,
    bearer_auth_header,
    join_url,
    parse_data_key_envelope,
    request_json,
)
from aiops_diagnostics.common.endpoint_policy import is_loopback_host

_EXPORT_RETRY = RetryPolicy(max_retries=2)


class DifyDslError(RuntimeError):
    """Base class for every way a pull can fail."""

    code = "DIFY_DSL_ERROR"


class DifyDslUnreachable(DifyDslError):
    """Dify could not be reached or did not answer."""

    code = "DIFY_DSL_UNREACHABLE"


class DifyDslAuthRejected(DifyDslError):
    """Dify rejected the credential (401/403)."""

    code = "DIFY_DSL_AUTH_REJECTED"


class DifyDslMalformed(DifyDslError):
    """The artifact is not a DSL we can map: bad YAML, or a value we will not guess at."""

    code = "DIFY_DSL_MALFORMED"


class DifyDslVersionUnsupported(DifyDslError):
    """The DSL version is outside the shape this mapping was written against."""

    code = "DIFY_DSL_VERSION_UNSUPPORTED"


#: The export route is the console one, and it needs the workspace the app
#: lives in: Dify's admin-key path resolves the tenant owner from this header
#: (``extensions/ext_login.py``). A session credential resolves it implicitly;
#: the header is what makes the same call work with a controlled credential.
DIFY_EXPORT_PATH = "/console/api/apps/{app_id}/export"
#: The console's per-dataset route. Needed because the **exported DSL does not
#: carry** the one field that lets us translate a dataset: measured on 41, a
#: dataset entry in the DSL is exactly ``{enabled, id}`` — no
#: ``external_knowledge_info``. The operator's handle (`kb-canary`) is only on
#: this route. See :func:`dataset_knowledge_id`.
#: App 详情：返回里 ``workflow.id`` 才是**已发布 workflow 的身份**。
#: 它不是 app_id，也不是 export 返回的 DSL 里任何字段 —— 三者必须分开取、分开存。
DIFY_APP_DETAIL_PATH = "/console/api/apps/{app_id}"
#: 把**草稿**发布成新的 workflow 版本。运营的「发布到 AI-Ops」必须自己走这一步：
#: Dify 的保存只写 draft，`export` 读的是 `app_model_config_id` 那个指针，
#: 而 `workflow.id` 在发布之前不会变 —— 不发布就等于把上一版又冻结了一次。
DIFY_WORKFLOW_PUBLISH_PATH = "/console/api/apps/{app_id}/workflows/publish"
DIFY_DATASET_PATH = "/console/api/datasets/{dataset_id}"
DIFY_WORKSPACE_HEADER = "X-WORKSPACE-ID"
#: 工作区的 API 工具提供方清单（#774）。返回的是 ``[{id, name, type, tools, …}]``，
#: 其中 ``id`` 就是工具条目里 ``provider_id`` 要填的那个 UUID —— 这是把「提供方
#: 名称」翻成「DSL 里那个 id」的唯一一跳。``type=api`` 只取自定义 API 提供方。
DIFY_TOOL_PROVIDERS_PATH = "/console/api/workspaces/current/tool-providers?type=api"

#: The only DSL shape we parse. A newer Dify is an unknown shape, and guessing
#: at an unknown shape is how a field silently lands in the wrong place — so a
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class DifyDslSource:
    """Where to pull one exported app from.

    ``api_key`` is a controlled credential: read from the private config at run
    time, never written to the repository, never logged, and ``repr=False``
    keeps it out of any repr or audit dump — the same discipline as
    ``GatewayServerSettings.company_source_key``.
    """

    base_url: str
    app_id: str
    api_key: str = field(repr=False, default="")
    workspace_id: str = ""
    timeout: float = 10.0


class DifyDslClient:
    """Minimal client for Dify's console export route."""

    def __init__(self, source: DifyDslSource) -> None:
        parsed = urlsplit(source.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Dify base URL must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Dify base URL must not carry credentials, a query or a fragment")
        # 这个请求带 console `api_key`：与 provider 同一条规则。
        if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
            raise ValueError("Dify base URL must use HTTPS unless it is a loopback address")
        if not _APP_ID.fullmatch(source.app_id.strip()):
            raise ValueError("Dify app id is invalid")
        if not 0.5 <= source.timeout <= 60:
            raise ValueError("Dify timeout must be between 0.5 and 60 seconds")
        if not source.api_key.strip():
            raise ValueError("Dify API key is required")
        self.source = source
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    def export_dsl(self, app_id: str | None = None, *, workflow_id: str | None = None) -> str:
        """Fetch the exported DSL text for one app."""
        target = (app_id or self.source.app_id).strip()
        if not _APP_ID.fullmatch(target):
            raise ValueError("Dify app id is invalid")
        headers = {
            "Accept": "application/json",
            "Authorization": bearer_auth_header(self.source.api_key),
        }
        if self.source.workspace_id:
            headers[DIFY_WORKSPACE_HEADER] = self.source.workspace_id
        return request_json(
            RequestSpec(
                url=join_url(self.base_url, DIFY_EXPORT_PATH.format(app_id=target))
                + ("?" + urlencode({"workflow_id": workflow_id}) if workflow_id else ""),
                headers=headers,
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=_export_envelope,
            retry=_EXPORT_RETRY,
        )

    def published_workflow_id(self, app_id: str | None = None) -> str:
        """这个 app **当前已发布** workflow 的身份。

        发布要切的那一版到底是不是眼前这份内容，只有这个 id 回答得了：
        `export` 返回的是 DSL 文本（没有 id），`app_id` 是应用不是图。
        实测形状（真 Dify 1.17.1）：`GET /console/api/apps/<app_id>` →
        `{"mode": "workflow", "workflow": {"id": "<uuid>", ...}}`。
        读不到就拒绝 —— 拿不到身份就没法声称"切到了这一版"。
        """
        target = (app_id or self.source.app_id).strip()
        if not _APP_ID.fullmatch(target):
            raise ValueError("Dify app id is invalid")
        headers = {
            "Accept": "application/json",
            "Authorization": bearer_auth_header(self.source.api_key),
        }
        if self.source.workspace_id:
            headers[DIFY_WORKSPACE_HEADER] = self.source.workspace_id
        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, DIFY_APP_DETAIL_PATH.format(app_id=target)),
                headers=headers,
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=lambda value, mapping: value,
            retry=_EXPORT_RETRY,
        )
        workflow = payload.get("workflow") if isinstance(payload, dict) else None
        identifier = workflow.get("id") if isinstance(workflow, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise DifyDslMalformed("Dify app 详情里没有已发布 workflow 的 id")
        return identifier.strip()

    def publish_workflow(self, app_id: str | None = None) -> bool:
        """把**已保存的草稿**发布成新的 workflow 版本。

        运营的工作流是：在控制台改 → 保存（只写 draft）→ 点「发布到 AI-Ops」。
        最后那一步必须**自己**完成 Dify 侧的发布，否则冻结的还是上一版，
        指针也会指向上一版 —— 而界面上一切看起来都成功了。

        Dify 对"草稿与已发布一致"的重复发布是幂等的，所以这里不先比对。
        """
        target = (app_id or self.source.app_id).strip()
        if not _APP_ID.fullmatch(target):
            raise ValueError("Dify app id is invalid")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": bearer_auth_header(self.source.api_key),
        }
        if self.source.workspace_id:
            headers[DIFY_WORKSPACE_HEADER] = self.source.workspace_id
        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, DIFY_WORKFLOW_PUBLISH_PATH.format(app_id=target)),
                method="POST",
                headers=headers,
                body=b"{}",
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=lambda value, mapping: value,
            retry=_EXPORT_RETRY,
        )
        # 成功形状是 `{"result": "success", ...}`；没有这个字段就当作没发布成。
        return isinstance(payload, dict) and payload.get("result") == "success"

    def tool_provider_ids(self) -> dict[str, str]:
        """``provider_name → ApiToolProvider.id`` for this workspace's API tools.

        The one hop the 3A tool rule needs and cannot get from the DSL. Dify's
        tool entry names a provider by ``provider_id``, and for a **custom API**
        provider that value is the ``ApiToolProvider`` row **UUID** — verified
        in 1.17.1's ``ToolManager.get_api_provider_controller``, which looks the
        provider up by ``ApiToolProvider.id == provider_id``. The DSL entry
        carries no provider *name* at all, so a name can only be read from the
        console — this call.

        The UUID is therefore resolved **per pull and never stored**: it appears
        in no config file, no registry row and no repository artifact, which is
        what keeps a rebuilt Dify instance (or a rebuilt provider) from
        invalidating anything already published. A stored UUID would be exactly
        the drift #772 E rules out.

        Provider names are unique per tenant (``unique_api_tool_provider`` on
        ``(name, tenant_id)``), so this map has no ambiguity to resolve.
        """
        headers = {
            "Accept": "application/json",
            "Authorization": bearer_auth_header(self.source.api_key),
        }
        if self.source.workspace_id:
            headers[DIFY_WORKSPACE_HEADER] = self.source.workspace_id
        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, DIFY_TOOL_PROVIDERS_PATH),
                headers=headers,
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=_tool_providers_envelope,
            retry=_EXPORT_RETRY,
        )
        return payload

    def dataset_knowledge_id(self, dataset_id: str) -> str:
        """The operator's handle for one Dify dataset — or a loud failure.

        This is the **only** bridge between the two id spaces, and it is one
        hop, not a translation: Dify's dataset carries the
        ``external_knowledge_id`` the operator typed into Dify's External
        Knowledge API form, and that same string is the key in our own
        ``AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS`` registry. The DSL does not
        carry it (measured: a DSL dataset entry is exactly ``{enabled, id}``),
        so it has to be read from here.

        Absent means one of two things, and both are failures rather than
        "no knowledge attached": the dataset is a **native** Dify dataset
        (nothing of ours is behind it), or the operator never registered the
        external API. Guessing either way would put a Dify id into
        ``knowledge_base_ids`` — the exact defect #741 fixed. So a missing or
        empty value raises.
        """
        target = dataset_id.strip()
        if not target:
            raise ValueError("Dify dataset id is empty")
        headers = {
            "Accept": "application/json",
            "Authorization": bearer_auth_header(self.source.api_key),
        }
        if self.source.workspace_id:
            headers[DIFY_WORKSPACE_HEADER] = self.source.workspace_id
        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, DIFY_DATASET_PATH.format(dataset_id=target)),
                headers=headers,
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=_dataset_envelope,
            retry=_EXPORT_RETRY,
        )
        external = (payload.get("external_knowledge_info") or {}).get("external_knowledge_id")
        if not isinstance(external, str) or not external.strip():
            raise DifyDslMalformed(
                f"Dify dataset {target} 没有 external_knowledge_id —— 它不是一个外接知识库"
                "（原生 dataset 背后没有我们的知识库）。请在 Dify 控制台把它改成挂"
                "「外接知识库 API」，或从 app 上移除这个数据集。"
            )
        return external.strip()


def _export_envelope(payload: Any, mapping: ErrorMapping) -> str:
    """The Dify export envelope: ``{"data": "<DSL YAML text>"}``.

    A missing or empty ``data`` means the export did not happen, and that has to
    be one explicit failure — not an object that gets mapped as if it were the
    DSL.
    """
    inner = parse_data_key_envelope(payload, mapping)
    if not isinstance(inner, str) or not inner.strip():
        raise mapping.invalid_envelope(HttpFailure("invalid_envelope", detail="missing export payload"))
    return inner


def _dataset_envelope(payload: Any, mapping: ErrorMapping) -> dict[str, Any]:
    """The dataset route answers with the object itself, not a ``data`` wrapper.

    Kept explicit (rather than reusing ``parse_data_key_envelope``) because the
    two routes really do differ: the export route wraps its DSL in ``data``,
    this one does not. Silently accepting either shape here would let a future
    change to one route go unnoticed on the other.
    """
    if not isinstance(payload, dict):
        raise mapping.invalid_envelope(
            HttpFailure("invalid_envelope", detail="dataset payload is not an object")
        )
    return payload


def _tool_providers_envelope(payload: Any, mapping: ErrorMapping) -> dict[str, str]:
    """``[{id, name, …}]`` → ``{name: id}``，形状不对就拒（不返回半个表）。

    拒而不是跳过的原因与知识库那条桥相同：一个解析不出的清单会让「提供方没找到」
    与「清单本身坏了」看起来一样，而前者会让运营去 Dify 里查一个其实配好的东西。
    """
    if not isinstance(payload, list):
        raise DifyDslMalformed(f"Dify 的工具提供方清单应当是数组，实际是 {type(payload).__name__}")
    table: dict[str, str] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise DifyDslMalformed(f"工具提供方条目 {index} 不是对象")
        name = item.get("name")
        identifier = item.get("id")
        if not isinstance(name, str) or not isinstance(identifier, str) or not name or not identifier:
            raise DifyDslMalformed(f"工具提供方条目 {index} 缺少 name 或 id")
        table[name] = identifier
    return table


def _error_mapping() -> ErrorMapping:
    """One clear failure signal per cause; a rejected credential is not an outage."""

    def rejected(failure: HttpFailure) -> DifyDslError:
        return DifyDslAuthRejected(f"Dify rejected the credential ({failure.status or failure.kind})")

    def unavailable(failure: HttpFailure) -> DifyDslError:
        return DifyDslUnreachable(f"Dify is unavailable: {failure.detail or failure.kind}")

    def malformed(failure: HttpFailure) -> DifyDslError:
        return DifyDslMalformed(f"Dify did not return a usable DSL: {failure.detail or failure.kind}")

    return ErrorMapping(
        auth_rejected=rejected,
        http_error=malformed,
        unavailable=unavailable,
        invalid_body=malformed,
        invalid_envelope=malformed,
    )
