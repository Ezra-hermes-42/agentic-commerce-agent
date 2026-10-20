"""The agent: natural language (or structured) request -> paid, reconciled order.

The default path is deterministic and dependency-free. An optional LLM hook can
parse messy free text, but it is off unless ``ACAGENT_LLM=1`` and it always
falls back to the deterministic parser (which also validates every SKU against
the catalog before any money is computed).

The agent is deliberately two-phase:

``create()``    request -> quote -> invoice -> PayPal order -> approval URL
``complete()``  (buyer approves) -> capture -> reconcile -> ledger

``run()`` wires them together, simulating approval for the mock client or
polling for approval with the real client.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, TextIO

from .catalog import Catalog, CatalogError
from .invoice import render_html, render_json
from .ledger import Ledger, ReconcileResult
from .mock_paypal import MockPayPal
from .paypal import (
    PayPalClient,
    PayPalError,
    approval_url_from_order,
)
from .quote import Quote, QuoteError, build_quote
from .transport import TransportError, UrllibTransport, json_request

ITEM_QTY_FIRST = re.compile(r"(\d+)\s*[x×]\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)")
ITEM_SKU_FIRST = re.compile(r"\b([A-Za-z][A-Za-z0-9_.\-]*)\s*[x×]\s*(\d+)\b")
CUSTOMER_RE = re.compile(r"\bfor\s+([^,;]+?)\s*[.!]?\s*$", re.IGNORECASE)


class AgentError(RuntimeError):
    """The order could not be completed; the message says which step failed."""


@dataclass
class AuditEvent:
    step: str
    detail: str
    ts: str

    def line(self, index: int) -> str:
        return f"  {index:02d}  {self.step:<22} {self.detail}"


@dataclass
class OrderResult:
    request_text: str | None
    customer: str | None
    quote: Quote
    invoice: dict
    invoice_html: str
    order: dict
    approval_url: str | None
    capture: dict | None = None
    reconciliation: ReconcileResult | None = None
    events: list[AuditEvent] = field(default_factory=list)
    mode: str = "mock"

    @property
    def order_id(self) -> str:
        return str(self.order.get("id"))

    @property
    def capture_id(self) -> str | None:
        return self.reconciliation.capture_id if self.reconciliation else None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "request": self.request_text,
            "customer": self.customer,
            "quote": self.quote.to_dict(),
            "invoice": self.invoice,
            "paypal_order_id": self.order_id,
            "approval_url": self.approval_url,
            "capture": self.capture,
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "audit": [{"step": e.step, "detail": e.detail, "ts": e.ts} for e in self.events],
        }


def default_payments() -> MockPayPal | PayPalClient:
    """Real sandbox client when credentials exist, otherwise the offline fake."""

    if os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"):
        return PayPalClient()
    return MockPayPal()


class CommerceAgent:
    def __init__(
        self,
        catalog: Catalog,
        *,
        payments: MockPayPal | PayPalClient | None = None,
        ledger: Ledger | None = None,
        clock: Callable[[], float] | None = None,
        transport: Any | None = None,
        approval_timeout: float | None = None,
        poll_interval: float = 2.0,
    ):
        self.catalog = catalog
        self.payments = payments or default_payments()
        self.ledger = ledger if ledger is not None else Ledger("acagent-ledger.jsonl")
        self.clock = clock or time.time
        self.transport = transport or UrllibTransport()
        self.mode = "mock" if isinstance(self.payments, MockPayPal) else "paypal"
        if approval_timeout is None:
            try:
                approval_timeout = float(os.environ.get("ACAGENT_APPROVAL_TIMEOUT", "0"))
            except ValueError:
                approval_timeout = 0.0
        self.approval_timeout = max(0.0, approval_timeout)
        self.poll_interval = max(0.1, poll_interval)

    # -- parsing --------------------------------------------------------
    def parse_request(self, request: str | Mapping[str, Any]) -> dict:
        """Return ``{"customer": str|None, "items": [{"sku", "qty"}], "source": ...}``."""

        if isinstance(request, Mapping):
            items = request.get("items")
            if not items:
                raise AgentError("structured request needs a non-empty 'items' list")
            customer = request.get("customer")
            return {
                "customer": customer.strip() if isinstance(customer, str) and customer.strip() else None,
                "items": list(items),
                "source": "structured",
            }
        if not isinstance(request, str) or not request.strip():
            raise AgentError("order request must be a non-empty string or a structured dict")

        text = request.strip()
        events: list[str] = []
        parsed = None
        if os.environ.get("ACAGENT_LLM") == "1":
            parsed = self._llm_parse(text, events)
        if parsed is None:
            parsed = self._text_parse(text)
        parsed["source"] = "llm" if parsed.pop("_llm", False) else "text"
        parsed["_notes"] = events
        return parsed

    def _text_parse(self, text: str) -> dict:
        items: list[dict] = []
        seen: set[str] = set()
        for match in ITEM_QTY_FIRST.finditer(text):
            qty, sku = int(match.group(1)), match.group(2)
            if sku.lower() in {"and", "for", "with"}:
                continue
            items.append({"sku": sku, "qty": qty})
            seen.add(sku)
        for match in ITEM_SKU_FIRST.finditer(text):
            sku, qty = match.group(1), int(match.group(2))
            if sku in seen or sku.lower() in {"and", "for", "with"}:
                continue
            items.append({"sku": sku, "qty": qty})
        if not items:
            raise AgentError(
                f"could not find any line items in {text!r}; try '2x widget-a and 1x widget-b for Acme Corp'"
            )

        customer = None
        match = CUSTOMER_RE.search(text)
        if match:
            customer = match.group(1).strip().rstrip(",;")
        return {"customer": customer, "items": items}

    def _llm_parse(self, text: str, notes: list[str]) -> dict | None:
        """Optional OpenAI-compatible parse. Any failure falls back to regex."""

        api_key = os.environ.get("ACAGENT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            notes.append("llm_parse_skipped: no ACAGENT_LLM_API_KEY/OPENAI_API_KEY")
            return None
        base = os.environ.get("ACAGENT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.environ.get("ACAGENT_LLM_MODEL", "gpt-4o-mini")
        skus = ", ".join(self.catalog.skus())
        try:
            status, data = json_request(
                self.transport,
                "POST",
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={
                    "model": model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Extract a purchase order as JSON: "
                                '{"customer": string|null, "items": [{"sku": string, "qty": integer}]}. '
                                f"Only these SKUs are valid: {skus}. No prose."
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                },
                timeout=30.0,
            )
            if status >= 400:
                notes.append(f"llm_parse_failed: HTTP {status}")
                return None
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(content)
            items = parsed.get("items")
            if not isinstance(items, list) or not items:
                notes.append("llm_parse_failed: no items in response")
                return None
            for item in items:
                if not self.catalog.has(str(item.get("sku", ""))):
                    notes.append(f"llm_parse_failed: unknown sku {item.get('sku')!r}")
                    return None
            notes.append(f"llm_parse_used: {model}")
            return {
                "customer": parsed.get("customer"),
                "items": items,
                "_llm": True,
            }
        except (TransportError, KeyError, IndexError, TypeError, ValueError) as exc:
            notes.append(f"llm_parse_failed: {type(exc).__name__}: {exc}")
            return None

    # -- phase 1: quote, invoice, order ---------------------------------
    def create(self, request: str | Mapping[str, Any], *, customer: str | None = None) -> OrderResult:
        parsed = self.parse_request(request)
        customer = customer or parsed.get("customer")
        events: list[AuditEvent] = []
        raw_text = request if isinstance(request, str) else None
        self._event(events, "request", f"source={parsed['source']} items={len(parsed['items'])}")
        for note in parsed.get("_notes", []):
            self._event(events, "parse_note", note)

        try:
            quote = build_quote(self.catalog, parsed["items"], customer=customer)
        except (QuoteError, CatalogError) as exc:
            raise AgentError(f"cannot price request: {exc}") from exc
        self._event(
            events,
            "quote",
            f"{quote.quote_id}  {quote.total_cents} {quote.currency} cents "
            f"({len(quote.lines)} lines, tax {quote.tax_cents})",
        )

        issued_at = datetime.fromtimestamp(self.clock(), timezone.utc)
        invoice = render_json(quote, issued_at=issued_at)
        invoice_html = render_html(quote, issued_at=issued_at)
        self._event(events, "invoice", f"{invoice['invoice_id']} total={invoice['total']} {quote.currency}")

        try:
            order = self.payments.create_order(quote)
        except PayPalError as exc:
            raise AgentError(f"PayPal create_order failed: {exc}") from exc
        approval_url = approval_url_from_order(order)
        if not approval_url and order.get("status") not in ("APPROVED", "COMPLETED"):
            raise AgentError(f"PayPal order {order.get('id')} has no approval link")
        if approval_url is None:
            self._event(
                events,
                "paypal_resumed",
                f"order {order['id']} already {order.get('status')}; no approval link needed",
            )
        self._event(events, "paypal_order", f"{order['id']} status={order.get('status')} request_id={quote.quote_id}")

        return OrderResult(
            request_text=raw_text,
            customer=customer,
            quote=quote,
            invoice=invoice,
            invoice_html=invoice_html,
            order=order,
            approval_url=approval_url,
            events=events,
            mode=self.mode,
        )

    # -- phase 2: approval, capture, reconciliation ---------------------
    def approve(self, result: OrderResult) -> dict:
        """Simulate (mock) or wait for (real sandbox) buyer approval."""

        order_id = result.order_id
        if hasattr(self.payments, "simulate_buyer_approval"):
            order = self.payments.simulate_buyer_approval(order_id)  # type: ignore[attr-defined]
            self._event(result.events, "buyer_approval", "simulated buyer approved order")
            result.order = order
            return order

        deadline = time.monotonic() + self.approval_timeout
        while True:
            order = self.payments.get_order(order_id)
            status = order.get("status")
            if status in ("APPROVED", "COMPLETED"):
                self._event(result.events, "buyer_approval", f"buyer approved (status={status})")
                result.order = order
                return order
            if time.monotonic() >= deadline:
                raise AgentError(
                    f"buyer has not approved order {order_id} (status={status}). "
                    f"Open {result.approval_url} and re-run with ACAGENT_APPROVAL_TIMEOUT set "
                    "higher, or call approve()/complete() again after approving."
                )
            time.sleep(self.poll_interval)

    def complete(self, result: OrderResult) -> OrderResult:
        order_id = result.order_id
        capture: dict
        try:
            capture = self.payments.capture_order(order_id)
            self._event(result.events, "capture", f"capture={self._capture_id(capture)} status=COMPLETED")
        except PayPalError as exc:
            if exc.issue == "ORDER_ALREADY_CAPTURED":
                self._event(
                    result.events,
                    "capture",
                    "PayPal reports ORDER_ALREADY_CAPTURED; reconciling the existing capture",
                )
                capture = self.payments.get_order(order_id)
            else:
                raise AgentError(f"PayPal capture failed: {exc}") from exc

        reconciliation = self.ledger.reconcile(order_id, result.invoice, capture)
        self._event(
            result.events,
            "reconcile",
            f"{reconciliation.status} expected={reconciliation.expected_cents} "
            f"captured={reconciliation.captured_cents} {reconciliation.currency or ''}".strip(),
        )
        if reconciliation.entry:
            self._event(
                result.events,
                "ledger",
                f"entry #{reconciliation.entry['seq']} hash={reconciliation.entry['hash'][:16]}…",
            )
        if not reconciliation.ok and reconciliation.status != "DUPLICATE_CAPTURE":
            reasons = ", ".join(reconciliation.reasons)
            raise AgentError(f"reconciliation rejected the capture: {reasons}")

        result.capture = capture
        result.reconciliation = reconciliation
        return result

    def run(self, request: str | Mapping[str, Any], *, customer: str | None = None) -> OrderResult:
        result = self.create(request, customer=customer)
        self.approve(result)
        return self.complete(result)

    # -- presentation ---------------------------------------------------
    def print_audit(self, result: OrderResult, stream: TextIO | None = None) -> None:
        stream = stream or sys.stdout
        print("AUDIT TRAIL", file=stream)
        for index, event in enumerate(result.events, start=1):
            print(event.line(index), file=stream)

    @staticmethod
    def _capture_id(capture: Mapping[str, Any]) -> str:
        units = capture.get("purchase_units") or []
        if units:
            captures = (units[0].get("payments") or {}).get("captures") or []
            if captures:
                return str(captures[0].get("id"))
        return str(capture.get("id", "?"))

    def _event(self, events: list[AuditEvent], step: str, detail: str) -> None:
        events.append(
            AuditEvent(
                step=step,
                detail=detail,
                ts=datetime.fromtimestamp(self.clock(), timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            )
        )
