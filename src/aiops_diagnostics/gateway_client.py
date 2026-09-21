from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from aiops_diagnostics.bounded_http import (
    JSON_CONTENT_TYPE,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    RetryPolicy,
    bearer_auth_header,
    json_body,
    parse_raw_envelope,
    request_json,
)
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
        headers = {"Accept": JSON_CONTENT_TYPE}
        if payload is not None:
            body = json_body(payload)
            headers["Content-Type"] = JSON_CONTENT_TYPE
        if authenticated:
            if not self.token:
                raise GatewayClientError("gateway token is not configured")
            headers["Authorization"] = bearer_auth_header(self.token)

        # Retry only idempotent GET requests for transient connection errors
        # (Tailscale/VPN blips, momentary unreachable Gateway). POST enroll and
        # create_run are non-idempotent: the Gateway acts before responding, so a
        # retry after a dropped connection could redeem a single-use enrollment
        # code twice or create a duplicate diagnostic run. HTTP errors
        # (404/401/5xx) are never retried: they reflect a deliberate Gateway
        # response, not a transport failure. When polling, wait_for_run forwards
        # its deadline so a slow connection cannot retry far past the timeout.
        # The jittered backoff itself now lives in the skeleton, so this is the
        # only implementation of it in the repository.
        def _http_error(failure: HttpFailure) -> Exception:
            return GatewayClientError(f"gateway HTTP {failure.status}: {failure.detail}")

        def _unavailable(failure: HttpFailure) -> Exception:
            # The class name is part of the observable message; the skeleton
            # carries it in ``detail`` for transport failures.
            return GatewayClientError(f"gateway connection failed: {failure.detail}")

        result = request_json(
            RequestSpec(
                url=self.base_url + path,
                method=method,
                headers=headers,
                body=body,
                timeout=self.timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=_http_error,
                http_error=_http_error,
                unavailable=_unavailable,
                invalid_body=lambda _f: GatewayClientError("gateway returned invalid JSON"),
                invalid_envelope=lambda _f: GatewayClientError("gateway returned an invalid response"),
            ),
            envelope=parse_raw_envelope,
            retry=RetryPolicy(
                max_retries=self.max_retries,
                base_delay_seconds=1.0,
                max_delay_seconds=8.0,
                jitter_seconds=1.0,
                methods=frozenset({"GET"}),
            ),
            deadline=deadline,
        )
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
