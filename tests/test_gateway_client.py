import json
from pathlib import Path

import pytest

from aiops_diagnostics.gateway_client import GatewayClient, GatewayClientError
from aiops_diagnostics.gateway_config import GatewayClientProfile
from aiops_diagnostics.gateway_tokens import load_profile, load_token, save_profile, save_token


def test_profile_and_file_token_store_are_private(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AIOPS_GATEWAY_TOKEN_STORE", "file")
    profile = GatewayClientProfile(
        name="default",
        base_url="https://gateway.example.test",
        device_id="dev-1",
        workspace_id="ops",
    )

    save_profile(profile)
    assert save_token("default", "aops-secret") == "private-file"
    assert load_profile("default").workspace_id == "ops"
    assert load_token("default") == "aops-secret"
    assert (tmp_path / "config" / "gateway-profiles" / "default.json").stat().st_mode & 0o077 == 0
    assert (tmp_path / "config" / "gateway-tokens" / "default.token").stat().st_mode & 0o077 == 0


def test_gateway_client_requires_https_for_remote_endpoints() -> None:
    with pytest.raises(ValueError):
        GatewayClient("http://gateway.example.test")
    with pytest.raises(GatewayClientError):
        GatewayClient("http://127.0.0.1:8787").list_runs()


def test_profile_name_cannot_escape_private_config_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AIOPS_GATEWAY_TOKEN_STORE", "file")

    with pytest.raises(ValueError):
        save_token("../outside", "aops-secret")
    with pytest.raises(ValueError):
        load_profile("../outside")


def _fake_response(payload: dict):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    return resp


def test_gateway_client_retries_transient_url_errors(monkeypatch) -> None:
    import urllib.error

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=3)
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("transient blip")
        return _fake_response({"runs": []})

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    assert client.list_runs() == []
    assert calls["n"] == 3  # two transient failures, then success


def test_gateway_client_gives_up_after_retries(monkeypatch) -> None:
    import urllib.error

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=2)

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    with pytest.raises(GatewayClientError):
        client.list_runs()


def test_gateway_client_does_not_retry_http_errors(monkeypatch) -> None:
    import urllib.error

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=3)
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    with pytest.raises(GatewayClientError):
        client.list_runs()
    assert calls["n"] == 1  # HTTP errors are not retried


def test_non_idempotent_post_is_not_retried(monkeypatch) -> None:
    """enroll/create_run act server-side before responding, so a retry after a
    dropped connection could redeem a single-use enrollment code twice or create
    a duplicate run. POST requests must fail fast instead of retrying."""
    import urllib.error

    client = GatewayClient("https://gateway.example.test")
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("transient blip")

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    with pytest.raises(GatewayClientError):
        client.enroll("enr_code", device_name="dev", platform="linux")
    assert calls["n"] == 1  # non-idempotent POST: never retried


def test_get_retries_connection_reset_during_read(monkeypatch) -> None:
    """A reset mid-response.read() is a transient transport error, not a
    deliberate Gateway response, and should be retried for idempotent GETs."""
    client = GatewayClient("https://gateway.example.test", token="t", max_retries=3)
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        resp = _fake_response({"runs": []})
        if calls["n"] < 2:
            resp.read.side_effect = ConnectionResetError("reset by peer")
        return resp

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    assert client.list_runs() == []
    assert calls["n"] == 2  # one reset during read, then success


def test_get_retries_incomplete_read(monkeypatch) -> None:
    """http.client.IncompleteRead (truncated body) is transient for idempotent
    GETs and must be retried, not propagated to the caller."""
    import http.client

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=3)
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        resp = _fake_response({"runs": []})
        if calls["n"] < 2:
            resp.read.side_effect = http.client.IncompleteRead(b"partial")
        return resp

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    assert client.list_runs() == []
    assert calls["n"] == 2


def test_negative_max_retries_is_clamped(monkeypatch) -> None:
    """A negative max_retries must not produce an empty retry loop that leaves
    the response unset and crashes json.loads(None) with TypeError."""
    import urllib.error

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=-1)
    assert client.max_retries == 0

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.sleep", lambda s: None)

    with pytest.raises(GatewayClientError):  # not TypeError
        client.list_runs()


def test_wait_for_run_stops_retrying_past_deadline(monkeypatch) -> None:
    """wait_for_run forwards its timeout as a deadline so a slow connection
    cannot retry far past the configured timeout. Without the deadline, a single
    list_events poll would retry max_retries+1 times (backoffs ~1+2+4+8+8=23s)
    before raising, overshooting a 10s timeout by ~13s."""
    import urllib.error

    client = GatewayClient("https://gateway.example.test", token="t", max_retries=5)
    calls = {"n": 0}
    clock = {"t": 0.0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr("aiops_diagnostics.gateway_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.gateway_client.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_client.time.sleep",
        lambda s: clock.__setitem__("t", clock["t"] + s),
    )

    with pytest.raises(GatewayClientError):
        client.wait_for_run("run-1", timeout_seconds=10)
    # Deadline (10s) bounds the retries; elapsed time stays well under the
    # unbounded ~23s, and get_run is never reached.
    assert clock["t"] < 20
    assert calls["n"] < 6
