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

Two boundaries are deliberate:

* ``agent_type`` and ``output_contract`` are the caller's, not the DSL's. Dify
  stores neither — they are our concepts — so a DSL that omits them is not a
  gap to guess at.
* The DSL's ``opening_statement`` and ``suggested_questions`` are **not
  consumed**. That copy is user-visible and language-specific, and its
  authoritative source is our own versioned content artifact (#585): Dify has
  no per-language variants at all, so reading them here would bind the agent to
  one language and bypass the artifact we own.

Failure is one signal, never a half-built draft: the DSL is fetched, parsed and
mapped in full *before* the first write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
) -> AgentConfig:
    """Map exported DSL text onto the ``AgentConfig`` fields we run on.

    Every field we do read is read explicitly, and a field that is absent or
    holds a value we cannot use raises instead of being defaulted — an app we
    cannot map is never silently half-imported. ``opening_statement`` and
    ``suggested_questions`` are not read at all; see the module docstring.
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

    return AgentConfig(
        agent_type=agent_type,
        prompt=_required_prompt(model_config),
        knowledge_base_ids=_knowledge_base_ids(model_config),
        model=_required_model(model_config),
        output_contract=output_contract or _default_output_contract(agent_type),
    )


def pull_agent_draft(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    description: str,
    agent_type: str,
    output_contract: str | None = None,
) -> Agent:
    """Pull ``source`` and land the result as a draft. Never publishes.

    The fetch, the parse and the mapping all complete before the first write,
    so any failure leaves the store exactly as it was — no half-built draft.
    """
    dsl_text = DifyDslClient(source).export_dsl()
    config = map_dsl_to_config(dsl_text, agent_type=agent_type, output_contract=output_contract)
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


def _knowledge_base_ids(model_config: dict[str, Any]) -> tuple[str, ...]:
    """Read the enabled dataset ids out of ``dataset_configs.datasets.datasets``.

    Absent means "no knowledge attached", which is a real state — both fixtures
    pulled from 36 carry exactly that — so it maps to an empty tuple. A *present
    but unreadable* value raises instead: treating it as empty would quietly
    drop a binding the operator configured.

    Note for whoever reviews a draft: Dify's chat frontend replaces
    ``dataset_configs`` wholesale, so a change made in that form keeps only
    ``retrieval_model`` and loses the ``datasets`` subtree. A later pull then
    sees no knowledge bound, which this mapping reports honestly as empty.
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
