"""Pull a Dify app's exported DSL and land it as one of our agent **drafts**.

Dify never pushes: editing an app in its console emits an in-process signal and
nothing else, so the only way to get a configured agent out of it is to pull.
The one endpoint that yields the configuration as an artifact is the console
export route (``GET /console/api/apps/<app_id>/export``) — the Service API has
no equivalent, and ``difyctl`` cannot reach the prompt or the model config
(PRD #577, verified against Dify 1.17.1 on host 36). It answers with a JSON
envelope whose ``data`` is the DSL as YAML text.

What this module is, and is not:

* It **is** the pull client and the DSL→``AgentConfig`` mapping. It lands the
  result as a **draft** through :class:`AgentManager` — the same role-checked,
  validated, optimistic-concurrency-respecting path the HTTP API and
  ``aiops-admin reconcile`` use. It **never** publishes: the publish gate is
  ours, not Dify's (PRD #577).
* It is **not** the tenant↔app registry, the Dify instance, or the publish
  action.

Three boundaries are deliberate:

* ``agent_type`` and ``output_contract`` are the caller's, not the DSL's. Dify
  stores neither — they are our concepts — so a DSL that omits them is not a
  gap to guess at.
* The DSL's ``opening_statement`` and ``suggested_questions`` are **not
  consumed**. That copy is user-visible and language-specific, and its
  authoritative source is our own versioned content artifact (#585): Dify has
  no per-language variants at all, so reading them here would bind the agent to
  one language and bypass the artifact we own.
* The DSL's ``dataset_configs.datasets[].id`` is **not consumed either**, and
  for a sharper reason: it is a Dify *dataset* UUID, while
  ``AgentConfig.knowledge_base_ids`` holds *our* knowledge-base ids. The two are
  different id spaces, so copying one into the other is a type error — the
  value is well-formed and meaningless at once. It is not hypothetical: on 41
  that value (``df0f1d4a-…``, Dify's ``aiops-41-kb-canary-debug`` external
  dataset) reached the publish gate as if it were a knowledge base and every
  ``admin pull-dify`` for a bound app failed with ``知识库 df0f1d4a-… 不存在``.
  The binding is therefore the **operator's explicit input**
  (``knowledge_base_ids``), and a DSL that declares a dataset while none was
  given fails loudly instead of publishing the wrong id.

Failure is one signal, never a half-built draft: the DSL is fetched, parsed and
mapped in full *before* the first write.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode, urlsplit

import yaml

from aiops_diagnostics.agent_lifecycle import Agent, AgentConfig, AgentManager, config_from_snapshot
from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    RetryPolicy,
    bearer_auth_header,
    join_url,
    parse_data_key_envelope,
    request_json,
)

#: Retrying the export is safe because it is a read (``RetryPolicy`` limits
#: retries to GET by default). A transient blip should not fail the operator's
#: action; a rejected credential still fails on the first response.
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
#: version bump fails loudly here and the mapping gets read again.
SUPPORTED_DSL_MAJOR = 0
SUPPORTED_DSL_MINOR = 7
_DSL_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
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


def map_dsl_to_config(
    dsl_text: str,
    *,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    dataset_resolver: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
) -> AgentConfig:
    """Map exported DSL text onto the ``AgentConfig`` fields we run on.

    Every field we do read is read explicitly, and a field that is absent or
    holds a value we cannot use raises instead of being defaulted — an app we
    cannot map is never silently half-imported. ``opening_statement`` and
    ``suggested_questions`` are not read at all; see the module docstring.

    ``knowledge_base_ids`` is the caller's, like ``agent_type``: the DSL's
    dataset ids are Dify's, not ours (module docstring). ``None`` means the
    caller did not state a binding, and a DSL that *does* declare one then
    fails loudly — the alternative is publishing a Dify UUID as a knowledge
    base. An explicit tuple (including ``()``) is taken as stated.

    ``dataset_resolver`` is how a declared dataset stops being a dead end: the
    caller injects a function that turns Dify's dataset id into **our**
    knowledge-base ids (via the operator's handle — see
    :meth:`DifyDslClient.dataset_knowledge_id`). Kept as a parameter rather
    than a call from here so this function stays pure and testable with no
    network. ``None`` keeps the old behaviour exactly: a declared dataset with
    nowhere to resolve it is a hard failure, never a silent copy.
    """
    try:
        document = yaml.safe_load(dsl_text)
    except yaml.YAMLError as exc:
        raise DifyDslMalformed(f"DSL is not valid YAML: {exc.__class__.__name__}") from exc
    if not isinstance(document, dict):
        raise DifyDslMalformed("DSL root must be an object")

    _require_supported_version(document.get("version"))
    kind = document.get("kind")
    if kind not in {None, "app"}:
        raise DifyDslMalformed(f"DSL kind must be 'app', got {kind!r}")

    model_config = document.get("model_config")
    if not isinstance(model_config, dict):
        raise DifyDslMalformed("DSL has no model_config object (only chat/completion apps carry one)")

    declared = _declared_dataset_ids(model_config)
    if knowledge_base_ids is None:
        if declared and dataset_resolver is not None:
            knowledge_base_ids = dataset_resolver(declared)
        elif declared:
            raise DifyDslMalformed(
                "DSL 声明了知识库数据集（"
                + ", ".join(declared)
                + "），但那是 Dify 的 dataset id，不是我们的知识库 id —— 两者不是同一个 id 空间。"
                "请显式给出 knowledge_base_ids（`admin pull-dify --knowledge-base-id`），不要按字面搬运。"
            )
        else:
            knowledge_base_ids = ()

    # 旧 Python 工具不再执行；原生工作流/节点清单单独发布。
    declared_tools = _declared_tools(model_config)
    if any(entry.enabled for entry in declared_tools):
        raise DifyDslMalformed("Python 内置工具已退役，请发布 Dify 工作流或 Java 节点清单")

    return AgentConfig(
        agent_type=agent_type,
        prompt=_required_prompt(model_config),
        knowledge_base_ids=tuple(knowledge_base_ids),
        model=_required_model(model_config),
        output_contract=output_contract or _default_output_contract(agent_type),
        # #585 的反转（PR-5）：`opening_statement` 现在**被读取**。它只覆盖 zh ——
        # 其余语言由制品回答（运行时没有翻译机制）。空/缺失 = 内层默认 ""，
        # 服务端回退制品，与新增该字段前的行为逐字相同。
        opening_statement=_optional_opening_statement(model_config),
        suggested_questions=_optional_suggested_questions(model_config),
        allowed_tools=(),
    )


def _resolve_declared_datasets(
    dataset_ids: tuple[str, ...],
    registry: Mapping[str, Any],
    client: DifyDslClient,
    *,
    tenant_id: str = "",
) -> tuple[str, ...]:
    """Translate Dify dataset ids into **our** knowledge-base ids, or refuse.

    Two hops, and every failure is a refusal rather than a fallback:

    * dataset id → the operator's handle (``external_knowledge_id``), read from
      the console. A **native** Dify dataset has no handle, so it is a refusal:
      there is nothing of ours behind it.
    * handle → our knowledge-base ids, through the same registry the inbound
      retrieval route uses. An unregistered handle is a refusal: we never guess
      which tenant a dataset belongs to.

    ``tenant_id`` is checked when given: a registry row for **another tenant**
    is refused rather than bound. The registry is a global table, and without
    this a dataset pointed at the wrong operator's handle would quietly attach
    that operator's knowledge base to this agent.

    All-or-nothing on purpose. Accepting three of four declared datasets would
    look like a successful pull while an operator's edit silently went missing,
    which is the failure this whole change exists to remove.

    Typed against ``Any`` rather than ``gateway_config.DifyKnowledgeBinding``
    so this module does not depend on the server-config module: it only ever
    reads ``knowledge_base_ids`` and ``tenant_id`` off the row.
    """
    resolved: list[str] = []
    for dataset_id in dataset_ids:
        handle = client.dataset_knowledge_id(dataset_id)
        binding = registry.get(handle)
        if binding is None:
            raise DifyDslMalformed(
                f"Dify dataset {dataset_id} 的外接知识库句柄是 {handle!r}，但 "
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS 里没有它 —— 我们不知道它对应哪个"
                "租户、哪些知识库。请先在 41 上登记这个句柄，或在 Dify 里改挂已登记的那一个。"
            )
        if tenant_id and str(getattr(binding, "tenant_id", "")) != tenant_id:
            raise DifyDslMalformed(
                f"Dify dataset {dataset_id} 的句柄 {handle!r} 登记在另一个租户名下"
                f"（{getattr(binding, 'tenant_id', '')}），而这个 agent 属于 {tenant_id} —— "
                "拒绝把别的租户的知识库挂上来。"
            )
        for knowledge_base_id in binding.knowledge_base_ids:
            if knowledge_base_id not in resolved:
                resolved.append(knowledge_base_id)
    return tuple(resolved)


def resolve_pulled_config(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    knowledge_registry: Mapping[str, Any] | None = None,
    tenant_id: str = "",
) -> AgentConfig:
    """Fetch, map and settle the config a pull would land — **no writes at all**.

    This is the single place the pulled config is decided. Dry-run and the real
    pull both call it, so what a dry-run reports is *the same object* the real
    pull would publish rather than a second implementation of the same rules.
    (A hand-rolled copy in the CLI's dry-run branch is exactly how the retained
    knowledge binding came to be reported as empty while the real pull kept it.)

    ``knowledge_base_ids`` is the operator's binding, not the DSL's; see
    :func:`map_dsl_to_config`. When it is ``None`` **and Dify declares no
    dataset**, the existing agent's binding is **kept**, never cleared: "this
    pull did not state a knowledge base" and "this agent has no knowledge base"
    are different statements, and only the second one should empty the field.
    Without that, pulling a dataset-free app would silently drop a live
    customer-service agent off the RAG path — the same silent-overwrite the
    manifest's prompt handling already guards against (#739).

    ``knowledge_registry`` is what makes an operator's Dify-side dataset change
    **take effect**. It is the *same* table the inbound retrieval route uses
    (:func:`parse_dify_knowledge_bindings`), read in the other direction: a
    dataset id becomes the operator's handle, and the handle names our
    knowledge-base ids. ``None`` keeps the pre-bridge behaviour — a declared
    dataset with nowhere to resolve is a hard failure, never a silent copy.
    """
    client = DifyDslClient(source)
    #: Set by the resolver below, which the mapper calls **only** when the DSL
    #: declares a dataset. That is how we tell "Dify said nothing" (keep the
    #: live binding) from "Dify named one" (its ids win) without parsing the
    #: DSL a second time and risking the two answers disagreeing.
    declared: list[tuple[str, ...]] = []

    def resolver(ids: tuple[str, ...]) -> tuple[str, ...]:
        declared.append(ids)
        return _resolve_declared_datasets(ids, knowledge_registry or {}, client, tenant_id=tenant_id)

    #: The ``aiops`` provider's UUID, resolved **once, here, from the console** —
    #: never from config (module docstring). Looked up lazily on first use so a
    #: DSL with no declared tools costs no extra console call; the console read
    #: happens only when the file actually names one.
    exported = client.export_dsl()
    existing = _find_by_name(manager, context, name)

    config = map_dsl_to_config(
        exported,
        agent_type=agent_type,
        output_contract=output_contract,
        knowledge_base_ids=knowledge_base_ids,
        dataset_resolver=resolver if knowledge_registry else None,
    )
    if knowledge_base_ids is None and not declared and existing is not None:
        # Only "Dify said nothing" keeps the live binding. When Dify *does*
        # declare a dataset, the resolved ids already won; keeping the live
        # ones on top would make the operator's edit invisible, which is the
        # whole point of this change.
        config = replace(config, knowledge_base_ids=existing.config.knowledge_base_ids)
    if existing is not None:
        # **``model`` 保持库中现值，永不从 Dify 覆盖。**
        #
        # 实测：运行时**不读** ``AgentConfig.model`` —— 真实模型来自 provider 配置
        # （``codex_runtime`` 读 ``provider.model``；全仓对 ``config.model`` 只有
        # 发布门与相等比较两处消费）。所以从 Dify 拉它**不改变线上模型**，
        # 却会经发布门产生一个用户可见的后果：运营在 Dify 里换了模型名而它不在
        # ``allowed_models`` 里时，**整个发布失败**；在允许列表里时，则是一次
        # 静默无效的改动 —— 两种都不是运营想要的答案。
        #
        # 因此这里的口径是「清单/库中值权威，Dify 那一侧不参与」：
        # 拉取不再动它，运营改它不会坏掉发布，也不会让谁以为线上模型变了。
        # 新建（库中不存在）时仍取 DSL 的值 —— 那时没有现值可保，且它要过发布门。
        config = replace(config, model=existing.config.model)
    return config


def pull_agent_draft(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    description: str,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    knowledge_registry: Mapping[str, Any] | None = None,
) -> Agent:
    """Pull ``source`` and land the result as a draft. Never publishes.

    The fetch, the parse and the mapping all complete before the first write,
    so any failure leaves the store exactly as it was — no half-built draft.
    The config itself comes from :func:`resolve_pulled_config`, which the
    dry-run path calls too, so the two cannot drift.
    """
    config = resolve_pulled_config(
        manager,
        context,
        source,
        name=name,
        agent_type=agent_type,
        output_contract=output_contract,
        knowledge_base_ids=knowledge_base_ids,
        knowledge_registry=knowledge_registry,
        tenant_id=str(getattr(context, "effective_tenant_id", "") or ""),
    )
    return converge_agent_draft(manager, context, config, name=name, description=description)


def converge_agent_draft(
    manager: AgentManager,
    context: Any,
    config: AgentConfig,
    *,
    name: str,
    description: str,
) -> Agent:
    """Bring the tenant's agent of this name to ``config`` as a draft.

    An existing draft is updated in place; an existing published agent is forked
    into a draft first, so the published snapshot an in-flight turn may be using
    stays immutable. Both go through ``expected_revision``, so a concurrent edit
    is a conflict rather than a silent overwrite. Converging twice is a no-op.
    """
    existing = _find_by_name(manager, context, name)
    if existing is None:
        return manager.create(context, name=name, description=description, config=config)

    draft = (
        existing
        if existing.status == "draft"
        else manager.fork_draft(context, existing.agent_id, expected_revision=existing.revision)
    )
    if draft.config == config and draft.description == description:
        return draft
    return manager.update(
        context,
        draft.agent_id,
        expected_revision=draft.revision,
        name=name,
        description=description,
        config=config,
    )


def _find_by_name(manager: AgentManager, context: Any, name: str) -> Agent | None:
    return next((agent for agent in manager.list(context) if agent.name == name), None)


def _require_supported_version(value: Any) -> None:
    if not isinstance(value, str):
        raise DifyDslMalformed(f"DSL has no version string, got {value!r}")
    match = _DSL_VERSION.match(value.strip())
    if not match:
        raise DifyDslMalformed(f"DSL version is not a semantic version: {value!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) != (SUPPORTED_DSL_MAJOR, SUPPORTED_DSL_MINOR):
        raise DifyDslVersionUnsupported(
            f"DSL version {value!r} is outside the supported "
            f"{SUPPORTED_DSL_MAJOR}.{SUPPORTED_DSL_MINOR}.x shape; "
            "re-read the mapping before pulling from this Dify version"
        )


def _required_prompt(model_config: dict[str, Any]) -> str:
    prompt = model_config.get("pre_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise DifyDslMalformed("DSL model_config.pre_prompt is empty; the prompt is the reason to pull")
    return prompt


def _required_model(model_config: dict[str, Any]) -> str:
    model = model_config.get("model")
    if not isinstance(model, dict):
        raise DifyDslMalformed(f"DSL model_config.model must be an object, got {type(model).__name__}")
    name = model.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DifyDslMalformed("DSL model_config.model.name is empty")
    return name


def _declared_dataset_ids(model_config: dict[str, Any]) -> tuple[str, ...]:
    """Read the enabled **Dify** dataset ids out of ``dataset_configs``.

    These are reported, never assigned: see the module docstring on why a Dify
    dataset id must not become one of our ``knowledge_base_ids``. Callers use
    this to say "the app declares knowledge, but you have not told me which of
    ours that is" instead of silently carrying the wrong id space forward.

    Absent means "no knowledge attached", which is a real state — both fixtures
    pulled from 36 carry exactly that. A *present but unreadable* value raises
    instead: treating it as empty would quietly lose a binding the operator
    configured. (Entry shape: flat ``{enabled, id}``, measured on Dify 1.17.1 —
    see the loop below.)

    Note for whoever reviews a draft: Dify's chat frontend replaces
    ``dataset_configs`` wholesale, so a change made in that form keeps only
    ``retrieval_model`` and loses the ``datasets`` subtree. A later pull then
    sees no knowledge declared, which this reports honestly as empty.
    """
    dataset_configs = model_config.get("dataset_configs")
    if dataset_configs is None:
        return ()
    if not isinstance(dataset_configs, dict):
        raise DifyDslMalformed("DSL model_config.dataset_configs must be an object")
    datasets = dataset_configs.get("datasets")
    if datasets is None:
        return ()
    if not isinstance(datasets, dict):
        raise DifyDslMalformed("DSL model_config.dataset_configs.datasets must be an object")
    entries = datasets.get("datasets")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise DifyDslMalformed("DSL dataset_configs.datasets.datasets must be a list")

    ids: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DifyDslMalformed(f"dataset entry {index} must be an object, got {type(entry).__name__}")
        # 形状**实测**自 Dify 1.17.1 的 `GET …/export`：条目是**平铺**的
        # `{"enabled": true, "id": "<uuid>"}`，**没有**嵌套的 `dataset` 对象。
        # 旧实现要求嵌套 ⇒ 对每一个真的挂了知识库的 app 都报「must carry a 'dataset'
        # object」。它能活到现在只是因为**从来没有一个 app 真的挂过**（两个 fixture 都
        # 连 `datasets` 键都没有），所以那条分支一次都没被走到。
        if not entry.get("enabled", False):
            continue
        dataset_id = entry.get("id")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise DifyDslMalformed(f"enabled dataset entry {index} has no id")
        ids.append(dataset_id)
    return tuple(ids)


def _optional_suggested_questions(model_config: dict[str, Any]) -> tuple[str, ...]:
    """Dify 的 ``suggested_questions``，缺失/None 时是空元组（= 不覆盖）。

    与开场白同一条纪律：不可读的值**抛错**，不静默当成"没给"。条目里的空串被跳过
    （Dify 的空行不是一条问题），但**整个字段类型不对**仍然抛。
    """
    value = model_config.get("suggested_questions")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DifyDslMalformed(
            f"DSL model_config.suggested_questions must be a list, got {type(value).__name__}"
        )
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise DifyDslMalformed(
                f"DSL model_config.suggested_questions[{index}] must be a string, got {type(item).__name__}"
            )
        if item.strip():
            out.append(item.strip())
    return tuple(out)


def _optional_opening_statement(model_config: dict[str, Any]) -> str:
    """Dify 的 ``opening_statement``，缺失/空交付时是空串（= 不覆盖）。

    ``None`` 是一个 Dify 会**写出来**的值（新建 app 时它是 null），而它和空串在这里
    同义：这个 app 没给开场白。**一个不可读的值仍然抛错** —— 静默当成"没给"会让
    运营写进去的开场白消失且没有任何信号。
    """
    value = model_config.get("opening_statement")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DifyDslMalformed(
            f"DSL model_config.opening_statement must be a string, got {type(value).__name__}"
        )
    return value.strip()


def _declared_tools(model_config: dict[str, Any]) -> tuple[Any, ...]:
    """The declared ``agent_mode.tools`` as :class:`DeclaredTool`, or refuse.

    Same contract as :func:`_declared_dataset_ids` — read as **reported**, never
    assigned — but it keeps the fields the 3A rule keys on (``provider_type``,
    ``provider_id``, ``enabled``) instead of flattening them to
    ``provider_id/tool_name``. 3A matches the provider by **UUID**, and a string
    that has been joined cannot be un-joined reliably (a UUID never contains
    ``/``, but the flattening is what made the first version of this rule
    unimplementable, so it is not repeated).
    """
    from aiops_diagnostics.dify_tools_catalog import DeclaredTool

    agent_mode = model_config.get("agent_mode")
    if agent_mode is None:
        return ()
    if not isinstance(agent_mode, dict):
        raise DifyDslMalformed("DSL model_config.agent_mode must be an object")
    tools = agent_mode.get("tools")
    if tools is None:
        return ()
    if not isinstance(tools, list):
        raise DifyDslMalformed("DSL model_config.agent_mode.tools must be a list")

    entries: list[DeclaredTool] = []
    for index, entry in enumerate(tools):
        if not isinstance(entry, dict):
            raise DifyDslMalformed(f"tool entry {index} must be an object, got {type(entry).__name__}")
        tool_name = entry.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise DifyDslMalformed(f"tool entry {index} has no tool_name")
        provider_id = entry.get("provider_id")
        entries.append(
            DeclaredTool(
                provider_type=str(entry.get("provider_type") or ""),
                provider_id=provider_id.strip() if isinstance(provider_id, str) else "",
                tool_name=tool_name.strip(),
                # Dify 的开关缺省为「开」：没写 enabled 的条目是有效条目，
                # 而不是一个需要运营去补字段的残缺声明。
                enabled=entry.get("enabled", True) is not False,
            )
        )
    return tuple(entries)


def _default_output_contract(agent_type: str) -> str:
    from aiops_diagnostics.agent_manifest import DEFAULT_OUTPUT_CONTRACTS

    try:
        return DEFAULT_OUTPUT_CONTRACTS[agent_type]
    except KeyError as exc:
        raise DifyDslMalformed(f"agent_type {agent_type!r} has no default output contract") from exc


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """One registry binding's result, named so the caller can print and count it.

    ``action`` is one of:

    * ``unchanged`` — Dify's prompt already equals the **published** version's
      prompt; nothing was written. This is the common case, and it is what makes
      re-running the command harmless — **not** a licence to put it on a timer
      (see :func:`sync_binding_from_dify` on why it must stay an explicit act).
    * ``synced`` — the prompt differed; a new version was frozen.
    * ``refused`` — the binding cannot be synced as declared (see ``note``).
    * ``failed`` — Dify or the publish gate rejected it (see ``note``).
    """

    tenant_id: str
    business_entry: str
    app_id: str
    agent_name: str
    action: str
    version_no: int | None = None
    note: str = ""
    #: **已发布 workflow 的身份**（不是 app_id，也不是 workflow_run_id）。
    #: 发布要把路由指针推到这一版，就必须把"这一版在 Dify 里到底是哪张图"一起记下来。
    #: 取不到时为空串：那是"不知道",调用方必须按未完成处理，不能拿 app_id 顶替。
    workflow_id: str = ""
    agent_id: str = ""
    route_scenario: str = ""


def _dsl_kind(dsl_text: str) -> str:
    """这份 DSL 是哪种应用：`workflow` / `chat` / `completion`。

    `map_dsl_to_config` 只吃 chat/completion（它们有 `model_config`）。workflow 应用的
    内容在 `workflow.graph` 里，**没有** `model_config` —— 拿它去走那条映射会得到
    `DSL has no model_config object`，而那看起来像"DSL 坏了"，其实是"这是另一种应用"。
    """
    try:
        document = yaml.safe_load(dsl_text)
    except Exception as exc:  # noqa: BLE001 - YAML 解析失败就是坏 DSL
        raise DifyDslMalformed(f"DSL is not valid YAML: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise DifyDslMalformed("DSL root must be an object")
    app = document.get("app")
    mode = app.get("mode") if isinstance(app, dict) else None
    if isinstance(mode, str) and mode.strip():
        return mode.strip()
    # 没有 `app.mode` 时退回看形状：有 `workflow.graph` 就是 workflow。
    return "workflow" if isinstance(document.get("workflow"), dict) else "chat"


def _merge_workflow_snapshot(previous: dict, workflow_body: dict, *, name: str) -> dict:
    """把 workflow 内容**叠加**到既有已发布快照上，而不是用它替换。

    这个 agent 可能同时在给客服问答（`qa_rag.select_customer_agent`）或运营诊断
    （`ops_agents`）供配置，它们从**已发布快照**里读 `agent_type` / `prompt` /
    `knowledge_base_ids`，并先按 `agent_type` 筛选。用一份只含 workflow 的快照覆盖，
    那些入口会当场选不到这个 agent —— 发布一条订单状态路由，顺手停掉了同一个绑定上
    的其他业务入口。
    """
    base = dict(previous) if isinstance(previous, dict) else {}
    return {**base, **workflow_body, "name": name}


def sync_workflow_from_dify(
    manager: AgentManager,
    context: Any,
    binding: Any,
    source: DifyDslSource,
    *,
    consumer_settings: dict | None = None,
    consumer_callback_base: str = "",
    consumer_libraries=None,
) -> SyncOutcome:
    """把一个**workflow**应用的当前内容冻结成新的已发布版本。

    与 `sync_binding_from_dify` 的差别只有一处，但它是概念上的：workflow 应用没有
    `model_config`，它的内容是一张图。所以这里冻结的快照是
    `{"workflow": <规范化后的 graph>, "workflow_id": ...}` —— **不是**一个 AgentConfig，
    也不假装是。版本仍然住在同一个 `agent_versions` 表里（内容不可变、可追发布者、
    revision 乐观锁都复用），只是 `snapshot` 的形状由应用类型决定。

    Dify 没有草稿区：保存即当前内容，所以"发布"= 把这个当前内容冻结。内容没变时
    按构造是 `unchanged`，重复点发布是 no-op。
    """
    client = DifyDslClient(source)
    existing = _find_by_name(manager, context, binding.agent_name)
    if existing is None or existing.status != "published" or existing.published_version is None:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            note="agent has no published configuration",
        )
    previous = manager.store.version(
        existing.agent_id, existing.tenant_id, existing.published_version
    ).snapshot
    candidate = workflow_content(client.export_dsl())
    if consumer_settings is not None:
        from aiops_diagnostics.consumer.consumer_flow_contract import publication_settings

        consumer_settings = publication_settings(
            candidate,
            consumer_settings,
            consumer_callback_base,
            require_split=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 2,
            require_picker=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 3,
            owned_libraries=consumer_libraries,
        )
    previous_workflow = previous.get("workflow_id")
    # **只有同一应用的历史版本才能参与去重。** 快照里的 `workflow_id` 属于**当时那个
    # 应用**；注册表把 agent 改绑到复制出来的新 app 之后，拿旧 id 到新 app 上导出会失败，
    # 于是"内容没变"的发布直接崩掉 —— 看起来像发布坏了，其实是把别人的历史当自己的。
    same_app = (
        previous.get("app_id") == binding.app_id and previous.get("workspace_id") == source.workspace_id
    )
    # Dify 原生 publish 每次新建版本；先确认内容相同且原不可变版本仍有效。
    if (
        same_app
        and previous.get("consumer_service") == consumer_settings
        and previous.get("workflow") == candidate
        and previous_workflow
        and workflow_content(client.export_dsl(workflow_id=previous_workflow)) == candidate
    ):
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            existing.published_version,
            workflow_id=previous_workflow,
            agent_id=existing.agent_id,
            route_scenario="consumer_service" if consumer_settings is not None else "",
        )
    if not client.publish_workflow(binding.app_id):
        raise DifyDslMalformed("Dify did not confirm the workflow publish")
    workflow_id = client.published_workflow_id(binding.app_id)
    frozen = workflow_content(client.export_dsl(workflow_id=workflow_id))
    if frozen != candidate:
        raise DifyDslMalformed("workflow changed during publish")
    snapshot_body = {
        "kind": "dify-workflow",
        "app_id": binding.app_id,
        "workspace_id": source.workspace_id,
        "workflow_id": workflow_id,
        "workflow": frozen,
    }
    if consumer_settings is not None:
        snapshot_body["consumer_service"] = consumer_settings
    agent = manager.fork_draft(context, existing.agent_id, expected_revision=existing.revision)
    version = manager.store.publish(
        agent.agent_id,
        binding.tenant_id,
        agent.revision,
        _merge_workflow_snapshot(previous, snapshot_body, name=binding.agent_name),
        published_by="dify-console",
    )
    return SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        binding.app_id,
        binding.agent_name,
        "synced",
        version.version_no,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        route_scenario="consumer_service" if consumer_settings is not None else "",
    )


def workflow_content(dsl_text: str) -> dict[str, Any]:
    """保留工作流行为及变量，剔除画布布局与选择状态。"""
    document = yaml.safe_load(dsl_text)
    workflow = document.get("workflow") if isinstance(document, dict) else None
    if not isinstance(workflow, dict) or not isinstance(workflow.get("graph"), dict):
        raise DifyDslMalformed("workflow DSL has no graph")
    graph = workflow["graph"]
    return {
        **{k: v for k, v in workflow.items() if k != "graph"},
        "graph": {
            "nodes": [
                {
                    "id": node.get("id"),
                    "data": {k: v for k, v in node.get("data", {}).items() if k != "selected"},
                }
                for node in graph.get("nodes", [])
            ],
            "edges": [
                {
                    k: v
                    for k, v in edge.items()
                    if k in {"source", "target", "sourceHandle", "targetHandle", "data"}
                }
                for edge in graph.get("edges", [])
            ],
        },
    }


def sync_binding_from_dify(
    manager: AgentManager,
    context: Any,
    binding: Any,
    source: DifyDslSource,
    *,
    dry_run: bool = False,
    knowledge_registry: Mapping[str, Any] | None = None,
) -> SyncOutcome:
    """Bring one registry binding's agent up to what **Dify currently holds**.

    Everything the pull needs is derived from the registry row and the agent it
    already names — ``app_id`` from the row, and ``agent_type``,
    ``output_contract``, ``description`` and ``knowledge_base_ids`` from the
    live agent. That is the whole point of the command: the operator edits a
    prompt in Dify, and *this* turns it into a published version without anyone
    re-typing seven parameters.

    **Idempotent by comparison, not by trust.** Dify has no draft area: saving
    a config is immediately what ``export`` returns, so Dify cannot tell us
    "this is unfinished". The only honest gate is therefore *change*: if the
    pulled prompt already equals the **published** version's prompt, nothing is
    written. Without this the command would fork and publish a new version on
    every run — several identical versions, and the one the runtime serves
    changes under it each time.

    Refusals — returned as ``refused`` and **written to nothing** — are:

    * the agent the row names does not exist in this tenant;
    * it is not ``published`` (a draft or disabled agent has no live version to
      compare against, and forking one would publish a state nobody froze);
    * the published snapshot cannot be read.

    Anything the **publish gate** rejects (model off the allowlist, a knowledge
    base that no longer resolves, a contract that does not match the type) is
    not a refusal here: it raises out of the converge/publish calls and the
    caller reports it as ``failed``. Keeping the two apart matters — ``refused``
    means "this binding is not syncable as declared", ``failed`` means "it was
    syncable and something upstream said no".

    Two disagreements are **not** checked, and deliberately so: the knowledge
    binding is taken from the live agent (never the DSL — module docstring), so
    it cannot differ; and the *type* cannot differ either, because
    ``AgentStore.update`` only touches rows whose ``status = 'draft'`` — a
    published agent's config is immutable until it is forked. A guard on either
    would be dead code that reads like a check.
    """
    from aiops_diagnostics.agent_lifecycle import AgentNotFound

    existing = _find_by_name(manager, context, binding.agent_name)
    if existing is None:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            None,
            f"库里没有这个 agent（{binding.agent_name}）—— 先按清单创建它",
        )
    if existing.status != "published" or existing.published_version is None:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            existing.published_version,
            f"agent 状态是 {existing.status}，没有可对比的已发布版本；先让它成为一个已发布版本",
        )

    # 发布要把路由指针推到"这一版在 Dify 里是哪张图"，所以身份要在**冻结版本之前**
    # 取到。取不到时**不阻断版本冻结**（CLI 的 `sync-from-dify` 与既有部署不依赖路由
    # 指针），但把 `workflow_id` 留空 —— 调用方必须按"未完成"处理，**不能**拿 app_id 顶替。
    workflow_id = ""
    workflow_identity_error = ""
    try:
        workflow_id = DifyDslClient(source).published_workflow_id(binding.app_id)
    except (DifyDslUnreachable, DifyDslAuthRejected, DifyDslMalformed) as exc:
        workflow_identity_error = f"读不到已发布 workflow 身份：{type(exc).__name__}"

    live = existing.config
    # ``knowledge_base_ids=None`` on purpose: it lets Dify's own dataset
    # declaration decide. Passing the live agent's ids here — which this did
    # until review — makes the knowledge bridge **dead on the sync path**,
    # because a non-``None`` value skips the resolver entirely. Sync is the
    # path an operator actually triggers, so that would have shipped a bridge
    # that never ran. ``resolve_pulled_config`` keeps the live binding by
    # itself when Dify declares nothing, which is the behaviour this preserves.
    config = resolve_pulled_config(
        manager,
        context,
        source,
        name=binding.agent_name,
        agent_type=live.agent_type,
        output_contract=live.output_contract,
        knowledge_base_ids=None,
        knowledge_registry=knowledge_registry,
        tenant_id=binding.tenant_id,
    )

    try:
        published = manager.version(context, existing.agent_id, existing.published_version)
    except AgentNotFound:  # pragma: no cover - published_version pointed at a missing row
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            existing.published_version,
            "已发布版本的快照读不到",
        )
    snapshot = published.snapshot
    # Compare **the whole config**, never a hand-picked pair of fields.
    #
    # This was `config.prompt == snapshot["prompt"] and config.model ==
    # snapshot["model"]`, which made every OTHER field invisible: a pull that
    # changed only `knowledge_base_ids` (or any field added later) reported
    # `unchanged` and published nothing, while the report said there was
    # nothing to do. `config_from_snapshot` is the inverse of `to_dict`, so a
    # field added to the dataclass joins this comparison automatically instead
    # of needing someone to remember to extend an `and` here.
    if config == config_from_snapshot(snapshot):
        # Dify holds exactly what the live version runs — the common case.
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            existing.published_version,
            workflow_identity_error,
            workflow_id=workflow_id,
        )

    if dry_run:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "synced",
            existing.published_version,
            "dry-run：Dify 与已发布版本不同，会冻结一个新版本",
            workflow_id=workflow_id,
        )

    # Converge the live agent onto the pulled config, then publish it. This is
    # the same fork→update→publish path `admin reconcile` and `/v1/agents` use.
    agent = converge_agent_draft(
        manager, context, config, name=binding.agent_name, description=existing.description
    )
    if agent.status != "draft":
        agent = manager.get(context, agent.agent_id)
    version = manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    return SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        binding.app_id,
        binding.agent_name,
        "synced",
        version.version_no,
        workflow_identity_error,
        workflow_id=workflow_id,
    )
