"""A thin HTTP abstraction.

Everything in the package talks to PayPal through this one function shape, so
tests can inject a fake transport and production code can use ``urllib`` without
either side knowing about the other.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping


class TransportError(RuntimeError):
    """Raised when the network call itself fails (DNS, TLS, timeout, ...)."""


@dataclass
class Response:
    status: int
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return {"raw": self.body}


class Transport:
    """Interface implemented by the real and fake transports."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        timeout: float = 30.0,
    ) -> Response:  # pragma: no cover - interface
        raise NotImplementedError


class UrllibTransport(Transport):
    """Standard-library transport. Raises :class:`TransportError` on network failure."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        timeout: float = 30.0,
    ) -> Response:
        data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(url, data=data, method=method.upper())
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Response(
                    status=response.status,
                    body=response.read().decode("utf-8", "replace"),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    url=url,
                )
        except urllib.error.HTTPError as exc:
            # PayPal puts machine-readable error details in 4xx/5xx bodies.
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return Response(
                status=exc.code,
                body=raw,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                url=url,
            )
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"{method} {url} timed out after {timeout}s") from exc


def json_request(
    transport: Transport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: Any = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """Send an optional JSON payload and decode a JSON response.

    Returns ``(status_code, decoded_body)`` for every HTTP status, including
    errors — callers decide what a 4xx means. Raises only on transport failure.
    """

    request_headers = {"Accept": "application/json"}
    body: str | None = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=False)
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    response = transport.request(
        method, url, headers=request_headers, body=body, timeout=timeout
    )
    return response.status, response.json()
