from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from aiops_diagnostics.gateway_config import GatewayClientProfile, canonical_gateway_url
from aiops_diagnostics.gateway_tokens import load_token, save_profile, save_token


class GatewayClientError(RuntimeError):
    """The remote gateway rejected or could not complete a request."""


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 20,
        max_retries: int = 4,
    ) -> None:
        self.base_url = canonical_gateway_url(base_url)
        self.token = token
        self.timeout = timeout
        # Retries only apply to idempotent GET requests. Clamp to a non-negative
        # budget so a negative value means "one attempt, no retries" rather than
        # an empty loop that leaves the response unset.
        self.max_retries = max(0, max_retries)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", authenticated=False)

    def enroll(self, code: str, *, device_name: str, platform: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/enroll",
            {"code": code, "device_name": device_name, "platform": platform},
            authenticated=False,
        )

    def create_run(
        self,
        *,
        problem: str,
        order_no: str | None = None,
        tenant_id: str | None = None,
        key_slot: str | None = None,
        provider: str | None = None,
        fixture_name: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/runs",
            {
                "problem": problem,
                "order_no": order_no,
                "tenant_id": tenant_id,
                "key_slot": key_slot,
                "provider": provider,
                "fixture_name": fixture_name,
            },
        )

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/v1/runs?limit={limit}")
        return list(payload.get("runs", []))

    def get_run(self, run_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{_path_part(run_id)}", deadline=deadline)

    def list_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/runs/{_path_part(run_id)}/events?after={after}&limit={limit}",
            deadline=deadline,
        )

    def list_evidence(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{_path_part(run_id)}/evidence")

    def wait_for_run(
        self,
        run_id: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        poll_seconds: float = 0.5,
        timeout_seconds: float = 1800,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        sequence = 0
        while time.monotonic() < deadline:
            event_payload = self.list_events(run_id, after=sequence, deadline=deadline)
            for event in event_payload.get("events", []):
                sequence = max(sequence, int(event.get("sequence", sequence)))
                if on_event:
                    on_event(event)
            run = self.get_run(run_id, deadline=deadline)
            if run.get("status") in {"diagnosed", "inconclusive", "blocked", "interrupted", "failed"}:
                return run
            time.sleep(poll_seconds)
        raise GatewayClientError("timed out waiting for gateway run")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self.token:
                raise GatewayClientError("gateway token is not configured")
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        # Retry only idempotent GET requests for transient connection errors
        # (Tailscale/VPN blips, momentary unreachable Gateway). POST enroll and
        # create_run are non-idempotent: the Gateway acts before responding, so a
        # retry after a dropped connection could redeem a single-use enrollment
        # code twice or create a duplicate diagnostic run. HTTP errors
        # (404/401/5xx) are never retried: they reflect a deliberate Gateway
        # response, not a transport failure. When polling, wait_for_run forwards
        # its deadline so a slow connection cannot retry far past the timeout.
        attempts = self.max_retries + 1 if method == "GET" else 1
        last_connection_error: Exception | None = None
        raw: str | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                last_connection_error = None
                break
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                raise GatewayClientError(f"gateway HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
                last_connection_error = exc
                past_deadline = deadline is not None and time.monotonic() >= deadline
                if attempt + 1 >= attempts or past_deadline:
                    break
                # Exponential backoff with jitter so simultaneous clients do not
                # retry in lockstep after a shared gateway restart.
                time.sleep(min(2**attempt, 8) + random.uniform(0, 1))
        if raw is None:
            raise GatewayClientError(
                f"gateway connection failed: {last_connection_error.__class__.__name__}"
            ) from last_connection_error
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayClientError("gateway returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GatewayClientError("gateway returned an invalid response")
        return result


def enroll_profile(
    base_url: str,
    code: str,
    *,
    profile_name: str,
    device_name: str,
    platform: str,
) -> tuple[GatewayClientProfile, str]:
    client = GatewayClient(base_url)
    response = client.enroll(code, device_name=device_name, platform=platform)
    profile = GatewayClientProfile(
        name=profile_name,
        base_url=client.base_url,
        device_id=str(response["device_id"]),
        workspace_id=str(response["workspace_id"]),
    )
    save_profile(profile)
    storage = save_token(profile_name, str(response["token"]))
    return profile, storage


def client_from_profile(profile: GatewayClientProfile) -> GatewayClient:
    return GatewayClient(profile.base_url, load_token(profile.name))


def _path_part(value: str) -> str:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise GatewayClientError("invalid gateway path identifier")
    return value


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "request rejected"
