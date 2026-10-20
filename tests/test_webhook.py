"""PayPal client tests against a fake transport: OAuth caching, headers, webhooks.

No network is touched. These tests cover the real client's request construction
and error mapping; they do not prove anything about the live PayPal sandbox.
"""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import dataclass, field
from unittest import mock

from acagent import Catalog, build_quote
from acagent.paypal import (
    MissingCredentials,
    PayPalClient,
    PayPalError,
    approval_url_from_order,
)
from acagent.transport import Response

API = "https://api-m.sandbox.paypal.com"


@dataclass
class FakeTransport:
    routes: list[tuple[str, str, int, dict]] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        for route_method, suffix, status, payload in self.routes:
            if method == route_method and url.endswith(suffix):
                return Response(
                    status=status,
                    body=json.dumps(payload),
                    headers={"content-type": "application/json"},
                    url=url,
                )
        raise AssertionError(f"no fake route for {method} {url}")

    def calls_to(self, suffix: str) -> list[dict]:
        return [r for r in self.requests if r["url"].endswith(suffix)]


TOKEN_ROUTE = ("POST", "/v1/oauth2/token", 200, {"access_token": "A21.token", "expires_in": 32400, "token_type": "Bearer"})
ORDER_ROUTE = (
    "POST",
    "/v2/checkout/orders",
    201,
    {
        "id": "5O190127TN364715T",
        "status": "CREATED",
        "links": [
            {"href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/5O190127TN364715T", "rel": "self"},
            {"href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T", "rel": "approve"},
        ],
    },
)

CATALOG = Catalog.from_dict(
    {
        "currency": "USD",
        "products": [
            {"id": "widget-a", "name": "Widget A", "unit_price_cents": 1299, "tax_rate_bps": 825},
            {"id": "widget-b", "name": "Widget B", "unit_price_cents": 450, "tax_rate_bps": 0},
        ],
    }
)
QUOTE = build_quote(CATALOG, [{"sku": "widget-a", "qty": 2}, {"sku": "widget-b", "qty": 1}], customer="Acme")

SIGNATURE_HEADERS = {
    "PayPal-Transmission-Id": "tx-123",
    "PayPal-Transmission-Time": "2026-10-20T10:00:00Z",
    "PayPal-Transmission-Sig": "sig-abc",
    "PayPal-Cert-Url": "https://api.paypal.com/v1/notifications/certs/CERT-360caa42",
    "PayPal-Auth-Algo": "SHA256withRSA",
}


def client(transport: FakeTransport, *, webhook_id: str | None = "WH-1", clock=None) -> PayPalClient:
    return PayPalClient(
        client_id="client-id",
        client_secret="client-secret",
        api_base=API,
        webhook_id=webhook_id,
        transport=transport,
        clock=clock or (lambda: 1000.0),
    )


class AuthAndOrdersTests(unittest.TestCase):
    def test_missing_credentials_raise_clear_error(self):
        with mock.patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""}):
            with self.assertRaises(MissingCredentials) as ctx:
                PayPalClient(transport=FakeTransport())
        self.assertIn("PAYPAL_CLIENT_ID", str(ctx.exception))

    def test_create_order_builds_orders_v2_payload_and_idempotency_header(self):
        transport = FakeTransport([TOKEN_ROUTE, ORDER_ROUTE])
        paypal = client(transport)
        order = paypal.create_order(QUOTE)

        self.assertEqual(order["id"], "5O190127TN364715T")
        self.assertEqual(approval_url_from_order(order), "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T")
        self.assertEqual(len(transport.calls_to("/v1/oauth2/token")), 1)
        self.assertEqual(transport.calls_to("/v1/oauth2/token")[0]["body"], "grant_type=client_credentials")

        call = transport.calls_to("/v2/checkout/orders")[0]
        self.assertEqual(call["headers"]["PayPal-Request-Id"], QUOTE.quote_id)
        self.assertEqual(call["headers"]["Authorization"], "Bearer A21.token")
        payload = json.loads(call["body"])
        unit = payload["purchase_units"][0]
        self.assertEqual(payload["intent"], "CAPTURE")
        self.assertEqual(unit["amount"]["currency_code"], "USD")
        self.assertEqual(unit["amount"]["value"], "32.62")
        self.assertEqual(unit["amount"]["breakdown"]["item_total"]["value"], "30.48")
        self.assertEqual(unit["amount"]["breakdown"]["tax_total"]["value"], "2.14")
        self.assertEqual(unit["reference_id"], QUOTE.quote_id)
        self.assertEqual([item["sku"] for item in unit["items"]], ["widget-a", "widget-b"])
        self.assertEqual(unit["items"][0]["quantity"], "2")

    def test_token_is_cached_until_near_expiry(self):
        now = [1000.0]
        transport = FakeTransport(
            [
                TOKEN_ROUTE,
                ORDER_ROUTE,
                ("GET", "/v2/checkout/orders/5O190127TN364715T", 200, {"id": "5O190127TN364715T", "status": "APPROVED"}),
            ]
        )
        paypal = client(transport, clock=lambda: now[0])
        paypal.create_order(QUOTE)
        paypal.get_order("5O190127TN364715T")
        self.assertEqual(len(transport.calls_to("/v1/oauth2/token")), 1)

        now[0] = 1000.0 + 32400 - 30  # inside the 60s safety window
        paypal.get_order("5O190127TN364715T")
        self.assertEqual(len(transport.calls_to("/v1/oauth2/token")), 2)

    def test_capture_uses_idempotency_header_and_returns_payload(self):
        transport = FakeTransport(
            [
                TOKEN_ROUTE,
                (
                    "POST",
                    "/v2/checkout/orders/5O190127TN364715T/capture",
                    201,
                    {"id": "5O190127TN364715T", "status": "COMPLETED"},
                ),
            ]
        )
        data = client(transport).capture_order("5O190127TN364715T")
        self.assertEqual(data["status"], "COMPLETED")
        call = transport.calls_to("/v2/checkout/orders/5O190127TN364715T/capture")[0]
        self.assertEqual(call["headers"]["PayPal-Request-Id"], "capture-5O190127TN364715T")

    def test_api_error_becomes_paypal_error_with_issue(self):
        transport = FakeTransport(
            [
                TOKEN_ROUTE,
                (
                    "POST",
                    "/v2/checkout/orders/X/capture",
                    422,
                    {
                        "name": "UNPROCESSABLE_ENTITY",
                        "message": "Order already captured.",
                        "details": [{"issue": "ORDER_ALREADY_CAPTURED"}],
                        "debug_id": "dbg-1",
                    },
                ),
            ]
        )
        with self.assertRaises(PayPalError) as ctx:
            client(transport).capture_order("X")
        error = ctx.exception
        self.assertEqual(error.status, 422)
        self.assertEqual(error.issue, "ORDER_ALREADY_CAPTURED")
        self.assertIn("ORDER_ALREADY_CAPTURED", str(error))
        self.assertIn("dbg-1", str(error))


class WebhookTests(unittest.TestCase):
    EVENT = {"id": "WH-EVT-1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "ORDER-1"}}
    BODY = json.dumps(EVENT)

    def _verify_routes(self, status_word: str):
        return [
            TOKEN_ROUTE,
            ("POST", "/v1/notifications/verify-webhook-signature", 200, {"verification_status": status_word}),
        ]

    def test_signature_accepted(self):
        transport = FakeTransport(self._verify_routes("SUCCESS"))
        self.assertTrue(client(transport).verify_webhook_signature(SIGNATURE_HEADERS, self.BODY))

        call = transport.calls_to("/v1/notifications/verify-webhook-signature")[0]
        payload = json.loads(call["body"])
        self.assertEqual(payload["webhook_id"], "WH-1")
        self.assertEqual(payload["webhook_event"], self.EVENT)
        self.assertEqual(payload["transmission_id"], "tx-123")
        self.assertEqual(payload["transmission_sig"], "sig-abc")
        self.assertEqual(payload["auth_algo"], "SHA256withRSA")
        self.assertEqual(payload["cert_url"], SIGNATURE_HEADERS["PayPal-Cert-Url"])

    def test_signature_rejected(self):
        transport = FakeTransport(self._verify_routes("FAILURE"))
        self.assertFalse(client(transport).verify_webhook_signature(SIGNATURE_HEADERS, self.BODY))

    def test_header_names_are_case_insensitive(self):
        transport = FakeTransport(self._verify_routes("SUCCESS"))
        lower = {k.lower(): v for k, v in SIGNATURE_HEADERS.items()}
        self.assertTrue(client(transport).verify_webhook_signature(lower, self.BODY))

    def test_missing_headers_rejected_without_network_call(self):
        transport = FakeTransport([])
        self.assertFalse(client(transport).verify_webhook_signature({"paypal-transmission-id": "tx-1"}, self.BODY))
        self.assertEqual(transport.requests, [])

    def test_malformed_body_rejected(self):
        transport = FakeTransport([])
        self.assertFalse(client(transport).verify_webhook_signature(SIGNATURE_HEADERS, "{not json"))
        self.assertEqual(transport.requests, [])

    def test_missing_webhook_id_is_a_configuration_error(self):
        transport = FakeTransport([])
        with self.assertRaises(PayPalError) as ctx:
            client(transport, webhook_id=None).verify_webhook_signature(SIGNATURE_HEADERS, self.BODY)
        self.assertIn("PAYPAL_WEBHOOK_ID", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
