"""Product catalog loading and validation.

A catalog is a JSON document::

    {
      "currency": "USD",
      "products": [
        {"id": "widget-a", "name": "Widget A", "unit_price_cents": 1299,
         "currency": "USD", "tax_rate_bps": 825}
      ]
    }

Every problem is reported as :class:`CatalogError` with a message naming the
offending field, because a catalog is data supplied by a human (or another
agent) and silent coercion of prices is how money bugs happen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class CatalogError(ValueError):
    """Raised when a catalog is malformed or a SKU cannot be resolved."""


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{field} must be a non-empty string, got {value!r}")
    return value.strip()


def _require_int(value: Any, field: str, *, minimum: int, maximum: int | None = None) -> int:
    # bool is a subclass of int; True is never a price.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{field} must be an integer, got {value!r}")
    if value < minimum:
        raise CatalogError(f"{field} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise CatalogError(f"{field} must be <= {maximum}, got {value}")
    return value


def _require_currency(value: Any, field: str) -> str:
    currency = _require_str(value, field)
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise CatalogError(
            f"{field} must be a 3-letter uppercase ISO-4217 code, got {currency!r}"
        )
    return currency


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price_cents: int
    currency: str = "USD"
    tax_rate_bps: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.sku,
            "name": self.name,
            "unit_price_cents": self.unit_price_cents,
            "currency": self.currency,
            "tax_rate_bps": self.tax_rate_bps,
        }


class Catalog:
    """An immutable, validated collection of products keyed by SKU."""

    def __init__(self, products: Iterable[Product], currency: str = "USD"):
        items = list(products)
        if not items:
            raise CatalogError("catalog must contain at least one product")
        currency = _require_currency(currency, "catalog.currency")
        by_sku: dict[str, Product] = {}
        for product in items:
            if product.currency != currency:
                raise CatalogError(
                    f"product {product.sku!r} currency {product.currency} does not match "
                    f"catalog currency {currency}"
                )
            if product.sku in by_sku:
                raise CatalogError(f"duplicate product id {product.sku!r}")
            by_sku[product.sku] = product
        self._products = tuple(by_sku[sku] for sku in sorted(by_sku))
        self._by_sku = by_sku
        self.currency = currency

    # -- construction ---------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "Catalog":
        if not isinstance(data, dict):
            raise CatalogError(f"catalog must be a JSON object, got {type(data).__name__}")
        raw_products = data.get("products")
        if not isinstance(raw_products, list):
            raise CatalogError("catalog.products must be a list")
        currency = _require_currency(data.get("currency", "USD"), "catalog.currency")
        products = []
        for index, raw in enumerate(raw_products):
            where = f"catalog.products[{index}]"
            if not isinstance(raw, dict):
                raise CatalogError(f"{where} must be an object")
            sku = _require_str(raw.get("id"), f"{where}.id")
            products.append(
                Product(
                    sku=sku,
                    name=_require_str(raw.get("name"), f"{where}.name"),
                    unit_price_cents=_require_int(
                        raw.get("unit_price_cents"), f"{where}.unit_price_cents", minimum=0
                    ),
                    currency=_require_currency(
                        raw.get("currency", currency), f"{where}.currency"
                    ),
                    tax_rate_bps=_require_int(
                        raw.get("tax_rate_bps", 0),
                        f"{where}.tax_rate_bps",
                        minimum=0,
                        maximum=10_000,
                    ),
                )
            )
        return cls(products, currency=currency)

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CatalogError(f"cannot read catalog {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"catalog {path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    # -- access ---------------------------------------------------------
    def get(self, sku: str) -> Product:
        try:
            return self._by_sku[sku]
        except KeyError:
            known = ", ".join(sorted(self._by_sku)) or "<none>"
            raise CatalogError(f"unknown sku {sku!r}; known skus: {known}") from None

    def has(self, sku: str) -> bool:
        return sku in self._by_sku

    def skus(self) -> tuple[str, ...]:
        return tuple(self._by_sku)

    def products(self) -> tuple[Product, ...]:
        return self._products

    def __len__(self) -> int:
        return len(self._by_sku)

    def __contains__(self, sku: object) -> bool:
        return sku in self._by_sku

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Catalog({len(self._by_sku)} products, currency={self.currency})"
