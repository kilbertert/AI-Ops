"""出站端点 URL 的安全规则 —— 一处定义、跨端点一致（#754）。

「https 哪里都行，http 只给回环」这句话此前被逐字抄了五份、带五条不同文案；另有五类
**携带服务凭据**的端点整条不检查它。而「什么算回环」还有第二种渲染，对同一个输入给出
相反答案（字面量集合放行 `localhost` 但拒绝 `127.0.0.2`；`is_loopback` 正好相反）。

这里钉三件事：

1. `is_loopback_host` 是「本机」的唯一答案，两种旧渲染的**并集**；
2. 十个端点对同一组远端明文 URL 一律拒绝、对同一组回环 URL 一律接受；
3. 全仓不再出现第二份字面量集合。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops_diagnostics.common.endpoint_policy import is_loopback_host, require_secure_endpoint

SRC = Path(__file__).resolve().parents[2] / "src" / "aiops_diagnostics"


# ── 「本机」的定义 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1", "127.9.9.9"],
)
def test_these_hosts_are_loopback(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["kb.local", "10.0.0.5", "example.com", "", None, "256.0.0.1"])
def test_these_hosts_are_not_loopback(host: str | None) -> None:
    assert is_loopback_host(host) is False


def test_the_two_old_renderings_used_to_disagree_on_these_inputs() -> None:
    """各取一个「只有一种旧渲染能认出来」的输入，钉住并集的选择。

    - `127.0.0.2`：整个 `127/8` 都是回环，字面量集合只认 `127.0.0.1`；
    - `localhost`：字面量集合认，`ipaddress.ip_address("localhost")` 抛 ValueError。
    """
    import ipaddress

    assert ipaddress.ip_address("127.0.0.2").is_loopback
    with pytest.raises(ValueError):
        ipaddress.ip_address("localhost")
    assert is_loopback_host("127.0.0.2") and is_loopback_host("localhost")


# ── require_secure_endpoint ────────────────────────────────────────────────
def test_remote_plaintext_is_rejected_and_loopback_plaintext_is_allowed() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        require_secure_endpoint("http://kb.local:8799", what="kb-service base URL")
    assert require_secure_endpoint("http://127.0.0.1:8799/", what="x") == "http://127.0.0.1:8799"
    assert require_secure_endpoint("https://example.com", what="x") == "https://example.com"


@pytest.mark.parametrize(
    "url",
    ["http://user:pw@h", "https://h/?a=1", "https://h/#f", "h://x", "not a url", "https://"],
)
def test_shape_violations_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        require_secure_endpoint(url, what="x")


# ── 跨端点一致 ───────────────────────────────────────────────────────────────
REMOTE_PLAINTEXT = "http://kb.local:8799"
LOOPBACK_PLAINTEXT = "http://127.0.0.1:8799"


def _build_endpoints(remote: str) -> list[tuple[str, object]]:
    """十个端点各自的「构造/校验」入口，全部是今天生产代码里的真实调用点。"""
    from aiops_diagnostics.common.config import DisSettings, ProviderConfig, UpmsSettings, canonical_provider_base_url
    from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource
    from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url
    from aiops_diagnostics.identity.caller_auth import IntrospectionSettings
    from aiops_diagnostics.identity.company_token_auth import CompanyTokenSettings
    from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient
    from aiops_diagnostics.publication.agent_debug import KbServiceKnowledgeClient

    return [
        ("provider", lambda: canonical_provider_base_url(remote)),
        ("introspection", lambda: IntrospectionSettings(url=remote, client_id="i", client_secret="s", audience="a").validate()),
        (
            "company check_token",
            lambda: CompanyTokenSettings(url=remote, client_id="i", client_secret="s").validate(),
        ),
        ("gateway client", lambda: canonical_gateway_url(remote)),
        ("upms", lambda: UpmsSettings(base_url=remote)),
        ("dis", lambda: DisSettings(base_url=remote)),
        ("kb-service", lambda: KbServiceClient(remote, tenant_id="t", service_token="svc")),
        ("kb-service (publish)", lambda: KbServiceKnowledgeClient(remote, tenant_id="t")),
        ("dify console", lambda: DifyDslClient(DifyDslSource(base_url=remote, app_id="app-1", api_key="k"))),
    ]


@pytest.mark.parametrize("name", [
    "provider", "introspection", "company check_token", "gateway client", "upms", "dis",
    "kb-service", "kb-service (publish)", "dify console",
])
def test_every_endpoint_rejects_the_same_remote_plaintext_url(name: str) -> None:
    builders = dict(_build_endpoints(REMOTE_PLAINTEXT))
    with pytest.raises(ValueError):
        builders[name]()


@pytest.mark.parametrize("name", [
    "provider", "introspection", "company check_token", "gateway client", "upms", "dis",
    "kb-service", "kb-service (publish)", "dify console",
])
def test_every_endpoint_accepts_the_same_loopback_plaintext_url(name: str) -> None:
    """回环明文一律接受 —— 五类端点此前根本不检查它是承重的，现在一致。"""
    builders = dict(_build_endpoints(LOOPBACK_PLAINTEXT))
    builders[name]()


# ── 结构守护：不再有第二份字面量集合 ──────────────────────────────────────────
def test_the_loopback_literal_set_exists_once() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "endpoint_policy.py" and '127.0.0.1", "::1' in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"还有第二份回环字面量集合：{offenders}"
