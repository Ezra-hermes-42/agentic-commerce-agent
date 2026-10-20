"""Deterministic quote engine.

All money is integer cents. No floats, anywhere: a quote for the same catalog
and the same line items always produces the same bytes, the same totals and the
same ``quote_id``. That idempotency is what makes retries safe downstream.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .catalog import Catalog, CatalogError, Product

QUOTE_VERSION = 1


class QuoteError(ValueError):
    """Raised when an order request cannot be turned into a quote."""


def tax_cents_for(subtotal_cents: int, rate_bps: int) -> int:
    """Tax in whole cents, rounded half-up, using integers only.

    ``rate_bps`` is basis points (825 == 8.25%). Half-up is the convention most
    tax authorities and invoices expect; Python's ``round()`` is half-to-even,
    which would quietly produce different invoices than a human would.
    """

    if isinstance(subtotal_cents, bool) or not isinstance(subtotal_cents, int):
        raise QuoteError(f"subtotal_cents must be an integer, got {subtotal_cents!r}")
    if subtotal_cents < 0:
        raise QuoteError(f"subtotal_cents must be >= 0, got {subtotal_cents}")
    if isinstance(rate_bps, bool) or not isinstance(rate_bps, int):
        raise QuoteError(f"tax rate must be integer basis points, got {rate_bps!r}")
    if not 0 <= rate_bps <= 10_000:
        raise QuoteError(f"tax rate must be within 0..10000 bps, got {rate_bps}")
    return (subtotal_cents * rate_bps + 5_000) // 10_000


def format_cents(cents: int) -> str:
    """``1234 -> '12.34'``; PayPal wants decimal strings, not floats."""

    if isinstance(cents, bool) or not isinstance(cents, int):
        raise QuoteError(f"amount must be integer cents, got {cents!r}")
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_cents(value: Any) -> int:
    """Parse a decimal money string such as ``'12.34'`` into integer cents.

    Accepts ints (already cents) and strings with at most two decimal places.
    Rejects floats outright so a JSON number like ``12.34`` cannot sneak
    binary rounding into the ledger.
    """

    if isinstance(value, bool):
        raise QuoteError(f"invalid amount {value!r}")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise QuoteError(f"invalid amount {value!r}: expected string or integer cents")
    text = value.strip()
    if not text:
        raise QuoteError("invalid amount: empty string")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if text.count(".") > 1:
        raise QuoteError(f"invalid amount {value!r}")
    whole, _, fraction = text.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise QuoteError(f"invalid amount {value!r}")
    if len(fraction) > 2:
        raise QuoteError(f"amount {value!r} has more than 2 decimal places")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
    return -cents if negative else cents


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 friendly."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class LineItem:
    sku: str
    name: str
    qty: int
    unit_price_cents: int
    tax_rate_bps: int
    subtotal_cents: int
    tax_cents: int

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.tax_cents

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "qty": self.qty,
            "unit_price_cents": self.unit_price_cents,
            "tax_rate_bps": self.tax_rate_bps,
            "subtotal_cents": self.subtotal_cents,
            "tax_cents": self.tax_cents,
            "line_total_cents": self.total_cents,
        }


@dataclass(frozen=True)
class Quote:
    quote_id: str
    currency: str
    lines: tuple[LineItem, ...]
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    customer: str | None = None

    @property
    def amount_value(self) -> str:
        return format_cents(self.total_cents)

    def canonical_payload(self) -> dict:
        """The exact payload the quote id is hashed from (no id, no timestamp)."""

        return {
            "version": QUOTE_VERSION,
            "currency": self.currency,
            "customer": self.customer,
            "lines": [
                {
                    "sku": line.sku,
                    "qty": line.qty,
                    "unit_price_cents": line.unit_price_cents,
                    "tax_rate_bps": line.tax_rate_bps,
                    "subtotal_cents": line.subtotal_cents,
                    "tax_cents": line.tax_cents,
                }
                for line in self.lines
            ],
            "subtotal_cents": self.subtotal_cents,
            "tax_cents": self.tax_cents,
            "total_cents": self.total_cents,
        }

    def to_dict(self) -> dict:
        payload = self.canonical_payload()
        payload["quote_id"] = self.quote_id
        payload["lines"] = [line.to_dict() for line in self.lines]
        return payload


def _normalise_items(items: Any) -> list[tuple[str, int]]:
    """Accept ``[{'sku','qty'}]``, ``[(sku, qty)]`` or ``{sku: qty}``."""

    if isinstance(items, Mapping):
        items = list(items.items())
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
        raise QuoteError("items must be a list of {sku, qty} entries")

    merged: dict[str, int] = {}
    order: list[str] = []
    for index, entry in enumerate(items):
        sku: Any
        qty: Any
        if isinstance(entry, Mapping):
            sku, qty = entry.get("sku"), entry.get("qty")
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            sku, qty = entry
        else:
            raise QuoteError(f"items[{index}] must be a {{sku, qty}} object or (sku, qty)")
        if not isinstance(sku, str) or not sku.strip():
            raise QuoteError(f"items[{index}].sku must be a non-empty string")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise QuoteError(f"items[{index}].qty must be an integer >= 1, got {qty!r}")
        sku = sku.strip()
        if sku not in merged:
            order.append(sku)
        merged[sku] = merged.get(sku, 0) + qty
    if not merged:
        raise QuoteError("an order needs at least one line item")
    return [(sku, merged[sku]) for sku in order]


def _build_line(product: Product, qty: int) -> LineItem:
    subtotal = product.unit_price_cents * qty
    return LineItem(
        sku=product.sku,
        name=product.name,
        qty=qty,
        unit_price_cents=product.unit_price_cents,
        tax_rate_bps=product.tax_rate_bps,
        subtotal_cents=subtotal,
        tax_cents=tax_cents_for(subtotal, product.tax_rate_bps),
    )


def build_quote(
    catalog: Catalog,
    items: Sequence[Any],
    *,
    customer: str | None = None,
) -> Quote:
    """Price ``items`` against ``catalog`` and return an idempotent quote.

    Lines are sorted by SKU before hashing, so ``[a, b]`` and ``[b, a]`` are the
    same quote; identical requests therefore collapse onto one quote id and one
    PayPal ``PayPal-Request-Id``.
    """

    pairs = _normalise_items(items)
    lines: list[LineItem] = []
    for sku, qty in pairs:
        try:
            product = catalog.get(sku)
        except CatalogError as exc:
            raise QuoteError(str(exc)) from exc
        lines.append(_build_line(product, qty))
    lines.sort(key=lambda line: line.sku)

    subtotal = sum(line.subtotal_cents for line in lines)
    tax = sum(line.tax_cents for line in lines)
    customer_name = customer.strip() if isinstance(customer, str) and customer.strip() else None

    draft = Quote(
        quote_id="",
        currency=catalog.currency,
        lines=tuple(lines),
        subtotal_cents=subtotal,
        tax_cents=tax,
        total_cents=subtotal + tax,
        customer=customer_name,
    )
    digest = hashlib.sha256(canonical_json(draft.canonical_payload()).encode("utf-8")).hexdigest()
    return Quote(
        quote_id=f"qte_{digest[:24]}",
        currency=draft.currency,
        lines=draft.lines,
        subtotal_cents=draft.subtotal_cents,
        tax_cents=draft.tax_cents,
        total_cents=draft.total_cents,
        customer=draft.customer,
    )
