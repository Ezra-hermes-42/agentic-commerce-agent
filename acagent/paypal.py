"""PayPal Orders v2 client (stdlib ``urllib`` only).

Implements exactly the four calls this project needs:

* ``POST /v1/oauth2/token``                              — OAuth2 client_credentials
* ``POST /v2/checkout/orders``                           — create an order
* ``GET  /v2/checkout/orders/{id}``                       — read an order
* ``POST /v2/checkout/orders/{id}/capture``               — capture payment
* ``POST /v1/notifications/verify-webhook-signature``     — verify a webhook

Nothing here has been exercised against a live sandbox: this repository ships
without PayPal credentials. See README "Limitations".
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable, Mapping

from .invoice import invoice_id
from .quote import Quote, format_cents
from .transport import Transport, UrllibTransport, json_request

DEFAULT_API_BASE = "https://api-m.sandbox.paypal.com"
TOKEN_SAFETY_SECONDS = 60.0


class PayPalError(RuntimeError):
    """A PayPal API call returned an error (or could not be attempted)."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}

    @property
    def issue(self) -> str | None:
        details = self.payload.get("details") or []
        if details and isinstance(details, list):
            return details[0].get("issue")
        return self.payload.get("name")


class MissingCredentials(PayPalError):
    """PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured."""


def order_payload(quote: Quote) -> dict:
    """Build the Orders v2 request body from a quote.

    ``amount.value`` is always the sum of ``item_total`` and ``tax_total`` —
    PayPal rejects orders where the breakdown does not add up, which is a
    useful server-side check on our own arithmetic.
    """

    items = [
        {
            "name": line.name,
            "sku": line.sku,
            "quantity": str(line.qty),
            "unit_amount": {
                "currency_code": quote.currency,
                "value": format_cents(line.unit_price_cents),
            },
            "category": "PHYSICAL_GOODS",
        }
        for line in quote.lines
    ]
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": quote.quote_id,
                "custom_id": quote.quote_id,
                "invoice_id": invoice_id(quote),
                "description": f"Quote {quote.quote_id}",
                "amount": {
                    "currency_code": quote.currency,
                    "value": format_cents(quote.total_cents),
                    "breakdown": {
                        "item_total": {
                            "currency_code": quote.currency,
                            "value": format_cents(quote.subtotal_cents),
                        },
                        "tax_total": {
                            "currency_code": quote.currency,
                            "value": format_cents(quote.tax_cents),
                        },
                    },
                },
                "items": items,
            }
        ],
        "application_context": {
            "brand_name": "Ezra Labs",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    }


def approval_url_from_order(order: Mapping[str, Any]) -> str | None:
    """Extract the buyer-facing approval URL from an Orders v2 response."""

    for link in order.get("links") or []:
        if isinstance(link, dict) and link.get("rel") in ("approve", "payer-action"):
            return link.get("href")
    return None


def captured_amount_from_response(capture: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return ``(value, currency, capture_id)`` from a capture response.

    Tolerant of the two shapes PayPal returns: a full order object (with
    ``purchase_units[0].payments.captures[0]``) and a bare capture object.
    Missing fields come back as ``None`` so the ledger can reject rather than
    crash on an unexpected payload.
    """

    capture_id = None
    if isinstance(capture, Mapping) and "purchase_units" in capture:
        units = capture.get("purchase_units") or []
        grabs = (units[0].get("payments") or {}).get("captures") if units else None
        if grabs:
            amount = grabs[0].get("amount") or {}
            capture_id = grabs[0].get("id")
            return amount.get("value"), amount.get("currency_code"), capture_id
        if units:
            amount = units[0].get("amount") or {}
            return amount.get("value"), amount.get("currency_code"), capture_id
        return None, None, None
    if isinstance(capture, Mapping):
        amount = capture.get("amount") or {}
        return amount.get("value"), amount.get("currency_code"), capture.get("id")
    return None, None, None


class PayPalClient:
    """Real Orders v2 client. One instance per process is fine; tokens are cached."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_base: str | None = None,
        webhook_id: str | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
        timeout: float = 30.0,
    ):
        self.client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")
        self.api_base = (
            api_base or os.environ.get("PAYPAL_API_BASE") or DEFAULT_API_BASE
        ).rstrip("/")
        self.webhook_id = webhook_id or os.environ.get("PAYPAL_WEBHOOK_ID")
        self.transport = transport or UrllibTransport()
        self.clock = clock or time.monotonic
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0
        if not self.client_id or not self.client_secret:
            raise MissingCredentials(
                "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set "
                "(or passed explicitly) to use the real PayPal client. "
                "Use MockPayPal for offline runs."
            )

    # -- auth -----------------------------------------------------------
    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached OAuth2 token, refreshing it shortly before expiry."""

        if (
            not force_refresh
            and self._token
            and self.clock() < self._token_expires_at - TOKEN_SAFETY_SECONDS
        ):
            return self._token

        response = self.transport.request(
            "POST",
            f"{self.api_base}/v1/oauth2/token",
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body="grant_type=client_credentials",
            timeout=self.timeout,
        )
        payload = response.json()
        if response.status >= 400 or not isinstance(payload, dict) or "access_token" not in payload:
            raise PayPalError(
                "failed to obtain PayPal access token",
                status=response.status,
                payload=payload if isinstance(payload, dict) else {"raw": response.body},
            )
        self._token = payload["access_token"]
        expires_in = payload.get("expires_in", 0)
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 0.0
        self._token_expires_at = self.clock() + max(expires_in, 0.0)
        return self._token

    # -- HTTP -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
        token = self.access_token()
        request_headers = {"Authorization": f"Bearer {token}"}
        request_headers.update(headers or {})
        status, data = json_request(
            self.transport,
            method,
            f"{self.api_base}{path}",
            headers=request_headers,
            payload=payload,
            timeout=self.timeout,
        )
        if status >= 400:
            message = f"PayPal {method} {path} failed with HTTP {status}"
            if isinstance(data, dict):
                name = data.get("name") or data.get("error")
                detail = data.get("message") or data.get("error_description")
                if name:
                    message += f": {name}"
                if detail:
                    message += f" ({detail})"
                issue = None
                details = data.get("details")
                if isinstance(details, list) and details and isinstance(details[0], dict):
                    issue = details[0].get("issue")
                if issue:
                    message += f" issue={issue}"
                if data.get("debug_id"):
                    message += f" [debug_id={data['debug_id']}]"
            raise PayPalError(message, status=status, payload=data)
        return data if isinstance(data, dict) else {"raw": data}

    # -- Orders v2 ------------------------------------------------------
    def create_order(self, quote: Quote, *, request_id: str | None = None) -> dict:
        """Create an order. ``PayPal-Request-Id`` defaults to the quote id.

        Repeating the same quote therefore returns the same order instead of
        creating a second one (PayPal's idempotency contract).
        """

        return self._request(
            "POST",
            "/v2/checkout/orders",
            payload=order_payload(quote),
            headers={
                "PayPal-Request-Id": request_id or quote.quote_id,
                "Prefer": "return=representation",
            },
        )

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, *, request_id: str | None = None) -> dict:
        return self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            payload={},
            headers={
                "PayPal-Request-Id": request_id or f"capture-{order_id}",
                "Prefer": "return=representation",
            },
        )

    # -- webhooks -------------------------------------------------------
    def verify_webhook_signature(self, headers: Mapping[str, str], body: str | bytes) -> bool:
        """Verify a webhook by asking PayPal to validate the signature.

        Header names are case-insensitive. Missing headers or a malformed body
        return ``False`` without a network call; a missing ``webhook_id`` is a
        configuration error and raises.
        """

        lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        transmission_id = lower.get("paypal-transmission-id")
        transmission_time = lower.get("paypal-transmission-time")
        transmission_sig = lower.get("paypal-transmission-sig")
        cert_url = lower.get("paypal-cert-url")
        auth_algo = lower.get("paypal-auth-algo")
        if not all((transmission_id, transmission_time, transmission_sig, cert_url, auth_algo)):
            return False
        if not self.webhook_id:
            raise PayPalError(
                "PAYPAL_WEBHOOK_ID (or webhook_id=...) is required to verify webhooks"
            )
        if isinstance(body, (bytes, bytearray)):
            try:
                body = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                return False
        try:
            event = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return False

        data = self._request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            payload={
                "auth_algo": auth_algo,
                "cert_url": cert_url,
                "transmission_id": transmission_id,
                "transmission_sig": transmission_sig,
                "transmission_time": transmission_time,
                "webhook_id": self.webhook_id,
                "webhook_event": event,
            },
        )
        return data.get("verification_status") == "SUCCESS"
