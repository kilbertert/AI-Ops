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
  ``aiops admin reconcile`` use. It **never** publishes: the publish gate is
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
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import yaml

from aiops_diagnostics.agent_lifecycle import Agent, AgentConfig, AgentManager
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

#: The export route is the console one, and it needs the workspace the app
#: lives in: Dify's admin-key path resolves the tenant owner from this header
#: (``extensions/ext_login.py``). A session credential resolves it implicitly;
#: the header is what makes the same call work with a controlled credential.
DIFY_EXPORT_PATH = "/console/api/apps/{app_id}/export"
DIFY_WORKSPACE_HEADER = "X-WORKSPACE-ID"

#: The only DSL shape we parse. A newer Dify is an unknown shape, and guessing
#: at an unknown shape is how a field silently lands in the wrong place — so a
#: version bump fails loudly here and the mapping gets read again.
SUPPORTED_DSL_MAJOR = 0
SUPPORTED_DSL_MINOR = 7
_DSL_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

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

    def export_dsl(self, app_id: str | None = None) -> str:
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
                url=join_url(self.base_url, DIFY_EXPORT_PATH.format(app_id=target)),
                headers=headers,
                timeout=self.source.timeout,
            ),
            mapping=_error_mapping(),
            envelope=_export_envelope,
            retry=_EXPORT_RETRY,
        )


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
        if declared:
            raise DifyDslMalformed(
                "DSL 声明了知识库数据集（"
                + ", ".join(declared)
                + "），但那是 Dify 的 dataset id，不是我们的知识库 id —— 两者不是同一个 id 空间。"
                "请显式给出 knowledge_base_ids（`admin pull-dify --knowledge-base-id`），不要按字面搬运。"
            )
        knowledge_base_ids = ()

    return AgentConfig(
        agent_type=agent_type,
        prompt=_required_prompt(model_config),
        knowledge_base_ids=tuple(knowledge_base_ids),
        model=_required_model(model_config),
        output_contract=output_contract or _default_output_contract(agent_type),
    )


def resolve_pulled_config(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
) -> AgentConfig:
    """Fetch, map and settle the config a pull would land — **no writes at all**.

    This is the single place the pulled config is decided. Dry-run and the real
    pull both call it, so what a dry-run reports is *the same object* the real
    pull would publish rather than a second implementation of the same rules.
    (A hand-rolled copy in the CLI's dry-run branch is exactly how the retained
    knowledge binding came to be reported as empty while the real pull kept it.)

    ``knowledge_base_ids`` is the operator's binding, not the DSL's; see
    :func:`map_dsl_to_config`. When it is ``None`` the existing agent's binding
    is **kept**, never cleared: "this pull did not state a knowledge base" and
    "this agent has no knowledge base" are different statements, and only the
    second one should empty the field. Without that, pulling a dataset-free app
    (the shape the production app now has) would silently drop a live
    customer-service agent off the RAG path — the same silent-overwrite the
    manifest's prompt handling already guards against (#739).
    """
    config = map_dsl_to_config(
        DifyDslClient(source).export_dsl(),
        agent_type=agent_type,
        output_contract=output_contract,
        knowledge_base_ids=knowledge_base_ids,
    )
    if knowledge_base_ids is None:
        existing = _find_by_name(manager, context, name)
        if existing is not None:
            config = replace(config, knowledge_base_ids=existing.config.knowledge_base_ids)
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
    configured.

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
        dataset = entry.get("dataset")
        if not isinstance(dataset, dict):
            raise DifyDslMalformed(f"dataset entry {index} must carry a 'dataset' object")
        if not dataset.get("enabled", False):
            continue
        dataset_id = dataset.get("id")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise DifyDslMalformed(f"enabled dataset entry {index} has no id")
        ids.append(dataset_id)
    return tuple(ids)


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


def sync_binding_from_dify(
    manager: AgentManager,
    context: Any,
    binding: Any,
    source: DifyDslSource,
    *,
    dry_run: bool = False,
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

    live = existing.config
    config = resolve_pulled_config(
        manager,
        context,
        source,
        name=binding.agent_name,
        agent_type=live.agent_type,
        output_contract=live.output_contract,
        knowledge_base_ids=live.knowledge_base_ids,
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
    live_prompt = str(snapshot.get("prompt") or "")
    live_model = str(snapshot.get("model") or "")
    if config.prompt == live_prompt and config.model == live_model:
        # Dify holds exactly what the live version runs — the common case.
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            existing.published_version,
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
    )
