"""An in-memory PayPal stand-in with the same interface as :class:`PayPalClient`.

This is what makes the repository runnable with no credentials and no network.
It is deliberately *stateful and strict*: you cannot capture an order the buyer
has not approved, and you cannot capture the same order twice — both mirror
real Orders v2 behaviour, so the ledger's duplicate handling is exercised by an
honest failure rather than an artificially permissive fake.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from .invoice import invoice_id
from .paypal import PayPalError, approval_url_from_order, order_payload
from .quote import Quote, format_cents


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _paypal_id(seed: str) -> str:
    """A plausible-looking 17-character PayPal id, deterministic per seed."""

    return hashlib.sha256(seed.encode("utf-8")).hexdigest().upper()[:17]


class MockPayPal:
    """Fake Orders v2 service. Everything lives in memory and is deep-copied out."""

    api_base = "mock://paypal"

    def __init__(self, *, webhook_secret: str = "mock-webhook-secret"):
        self.webhook_secret = webhook_secret
        self.orders: dict[str, dict] = {}
        self.calls: list[dict] = []
        self._idempotency: dict[str, str] = {}
        self._capture_ids: set[str] = set()

    # -- helpers --------------------------------------------------------
    def _record(self, method: str, target: str, **extra: Any) -> None:
        self.calls.append({"method": method, "target": target, **extra})

    def _get(self, order_id: str) -> dict:
        try:
            return self.orders[order_id]
        except KeyError:
            raise PayPalError(
                "RESOURCE_NOT_FOUND",
                status=404,
                payload={
                    "name": "RESOURCE_NOT_FOUND",
                    "message": f"Order {order_id} not found",
                    "details": [{"issue": "INVALID_RESOURCE_ID"}],
                },
            ) from None

    # -- interface ------------------------------------------------------
    def create_order(self, quote: Quote, *, request_id: str | None = None) -> dict:
        request_id = request_id or quote.quote_id
        self._record("POST", "/v2/checkout/orders", request_id=request_id)
        if request_id in self._idempotency:
            existing = self._idempotency[request_id]
            return copy.deepcopy(self.orders[existing])

        order_id = _paypal_id(f"order:{request_id}")
        payload = order_payload(quote)
        order = {
            "id": order_id,
            "status": "CREATED",
            "intent": payload["intent"],
            "purchase_units": [
                {
                    "reference_id": quote.quote_id,
                    "custom_id": quote.quote_id,
                    "invoice_id": invoice_id(quote),
                    "description": f"Quote {quote.quote_id}",
                    "amount": payload["purchase_units"][0]["amount"],
                    "items": [
                        {
                            **item,
                            "quantity": item["quantity"],
                        }
                        for item in payload["purchase_units"][0]["items"]
                    ],
                }
            ],
            "create_time": _now(),
            "links": [
                {
                    "href": f"{self.api_base}/v2/checkout/orders/{order_id}",
                    "rel": "self",
                    "method": "GET",
                },
                {
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                    "rel": "approve",
                    "method": "GET",
                },
                {
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                    "rel": "payer-action",
                    "method": "GET",
                },
            ],
        }
        self.orders[order_id] = order
        self._idempotency[request_id] = order_id
        return copy.deepcopy(order)

    def get_order(self, order_id: str) -> dict:
        self._record("GET", f"/v2/checkout/orders/{order_id}")
        return copy.deepcopy(self._get(order_id))

    def capture_order(self, order_id: str, *, request_id: str | None = None) -> dict:
        request_id = request_id or f"capture-{order_id}"
        self._record("POST", f"/v2/checkout/orders/{order_id}/capture", request_id=request_id)
        order = self._get(order_id)

        if order["status"] == "COMPLETED":
            raise PayPalError(
                "ORDER_ALREADY_CAPTURED",
                status=422,
                payload={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "Order already captured.",
                    "details": [
                        {
                            "issue": "ORDER_ALREADY_CAPTURED",
                            "description": "Order already captured.",
                        }
                    ],
                },
            )
        if order["status"] != "APPROVED":
            raise PayPalError(
                "ORDER_NOT_APPROVED",
                status=422,
                payload={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": f"Order {order_id} has not been approved by the buyer.",
                    "details": [
                        {"issue": "ORDER_NOT_APPROVED", "description": "Payer has not yet approved the Order."}
                    ],
                },
            )

        capture_id = _paypal_id(f"capture:{order_id}:{request_id}")
        if capture_id in self._capture_ids:
            capture_id = _paypal_id(f"capture:{order_id}:{request_id}:1")

        unit = order["purchase_units"][0]
        amount = copy.deepcopy(unit["amount"])
        gross_cents = int(amount["value"].split(".")[0]) * 100 + int(
            (amount["value"].split(".") + ["00"])[1].ljust(2, "0")
        )
        # 2.9% + $0.30, in integer cents, purely cosmetic for the fake receipt.
        fee_cents = (gross_cents * 29 + 500) // 1000 + 30
        capture = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": amount,
            "final_capture": True,
            "seller_protection": {"status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED"]},
            "seller_receivable_breakdown": {
                "gross_amount": copy.deepcopy(amount),
                "paypal_fee": {
                    "currency_code": amount["currency_code"],
                    "value": format_cents(fee_cents),
                },
                "net_amount": amount,
            },
            "create_time": _now(),
            "update_time": _now(),
            "links": [
                {
                    "href": f"{self.api_base}/v2/payments/captures/{capture_id}",
                    "rel": "self",
                    "method": "GET",
                }
            ],
        }
        self._capture_ids.add(capture_id)

        order["status"] = "COMPLETED"
        order["update_time"] = _now()
        order["purchase_units"][0]["payments"] = {"captures": [capture]}
        order["links"] = [
            {"href": f"{self.api_base}/v2/checkout/orders/{order_id}", "rel": "self", "method": "GET"}
        ]
        self._record("CAPTURE", capture_id, amount=amount["value"], currency=amount["currency_code"])
        return copy.deepcopy(order)

    def verify_webhook_signature(self, headers: Mapping[str, str], body: str | bytes) -> bool:
        lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._record("POST", "/v1/notifications/verify-webhook-signature")
        return lower.get("paypal-transmission-sig") == self.webhook_secret

    # -- buyer simulation (mock-only) -----------------------------------
    def simulate_buyer_approval(self, order_id: str, *, payer_email: str = "buyer@example.com") -> dict:
        """Stand in for the buyer clicking *Approve* in the PayPal window."""

        order = self._get(order_id)
        if order["status"] == "COMPLETED":
            return copy.deepcopy(order)
        if order["status"] != "CREATED":
            raise PayPalError(f"order {order_id} cannot be approved from status {order['status']}")
        order["status"] = "APPROVED"
        order["payer"] = {
            "name": {"given_name": "Test", "surname": "Buyer"},
            "email_address": payer_email,
            "payer_id": _paypal_id(f"payer:{payer_email}"),
        }
        order["update_time"] = _now()
        order["links"] = [
            {"href": f"{self.api_base}/v2/checkout/orders/{order_id}", "rel": "self", "method": "GET"},
            {
                "href": f"{self.api_base}/v2/checkout/orders/{order_id}/capture",
                "rel": "capture",
                "method": "POST",
            },
        ]
        self._record("BUYER_APPROVED", order_id)
        return copy.deepcopy(order)

    # -- convenience -----------------------------------------------------
    def approval_url(self, order: Mapping[str, Any]) -> str | None:
        return approval_url_from_order(order)
