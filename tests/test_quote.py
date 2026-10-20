"""Quote engine + invoice tests: integer cents, rounding, idempotency, validation."""

from __future__ import annotations

import unittest

from acagent.catalog import Catalog, CatalogError
from acagent.invoice import invoice_id, render_html, render_json
from acagent.quote import (
    QuoteError,
    build_quote,
    format_cents,
    parse_cents,
    tax_cents_for,
)

CATALOG = {
    "currency": "USD",
    "products": [
        {"id": "widget-a", "name": "Widget A", "unit_price_cents": 1999, "tax_rate_bps": 825},
        {"id": "widget-b", "name": "Widget B", "unit_price_cents": 100, "tax_rate_bps": 0},
        {"id": "penny", "name": "One Cent Thing", "unit_price_cents": 1, "tax_rate_bps": 5000},
        {"id": "free", "name": "Free Sample", "unit_price_cents": 0, "tax_rate_bps": 825},
    ],
}


class QuoteMathTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_dict(CATALOG)

    def test_tax_rounds_half_up_in_integer_cents(self):
        # 1 cent at 50% = 0.5 cents -> 1 cent (half-up), not 0 (banker's rounding).
        self.assertEqual(tax_cents_for(1, 5000), 1)
        self.assertEqual(tax_cents_for(3, 5000), 2)  # 1.5 -> 2
        self.assertEqual(tax_cents_for(1999, 825), 165)  # 164.9175 -> 165
        self.assertEqual(tax_cents_for(0, 825), 0)

    def test_line_and_total_arithmetic(self):
        quote = build_quote(self.catalog, [{"sku": "widget-a", "qty": 3}])
        line = quote.lines[0]
        self.assertEqual(line.subtotal_cents, 5997)
        self.assertEqual(line.tax_cents, 495)  # 5997 * 8.25% = 494.7525 -> 495
        self.assertEqual(quote.subtotal_cents, 5997)
        self.assertEqual(quote.tax_cents, 495)
        self.assertEqual(quote.total_cents, 6492)
        self.assertEqual(quote.amount_value, "64.92")

    def test_total_is_sum_of_parts(self):
        quote = build_quote(
            self.catalog,
            [{"sku": "widget-a", "qty": 2}, {"sku": "widget-b", "qty": 1}, {"sku": "penny", "qty": 1}],
        )
        self.assertEqual(quote.subtotal_cents, sum(line.subtotal_cents for line in quote.lines))
        self.assertEqual(quote.tax_cents, sum(line.tax_cents for line in quote.lines))
        self.assertEqual(quote.total_cents, quote.subtotal_cents + quote.tax_cents)
        self.assertEqual(quote.currency, "USD")

    def test_zero_priced_line_is_allowed(self):
        quote = build_quote(self.catalog, [{"sku": "free", "qty": 2}])
        self.assertEqual(quote.total_cents, 0)
        self.assertEqual(quote.amount_value, "0.00")

    def test_invalid_quantities_rejected(self):
        for bad in (0, -1, True, "2", 1.5, None):
            with self.subTest(qty=bad), self.assertRaises(QuoteError):
                build_quote(self.catalog, [{"sku": "widget-a", "qty": bad}])

    def test_unknown_sku_rejected(self):
        with self.assertRaises(QuoteError) as ctx:
            build_quote(self.catalog, [{"sku": "nope", "qty": 1}])
        self.assertIn("unknown sku", str(ctx.exception))

    def test_empty_order_rejected(self):
        with self.assertRaises(QuoteError):
            build_quote(self.catalog, [])


class QuoteIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_dict(CATALOG)

    def test_same_request_same_quote_id(self):
        first = build_quote(self.catalog, [{"sku": "widget-a", "qty": 2}], customer="Acme")
        second = build_quote(self.catalog, [{"sku": "widget-a", "qty": 2}], customer="Acme")
        self.assertEqual(first.quote_id, second.quote_id)
        self.assertTrue(first.quote_id.startswith("qte_"))
        self.assertEqual(len(first.quote_id), 4 + 24)

    def test_line_order_does_not_change_quote_id(self):
        one = build_quote(self.catalog, [{"sku": "widget-a", "qty": 1}, {"sku": "widget-b", "qty": 2}])
        two = build_quote(self.catalog, [{"sku": "widget-b", "qty": 2}, {"sku": "widget-a", "qty": 1}])
        self.assertEqual(one.quote_id, two.quote_id)
        self.assertEqual([line.sku for line in one.lines], ["widget-a", "widget-b"])

    def test_quantity_or_customer_changes_quote_id(self):
        base = build_quote(self.catalog, [{"sku": "widget-a", "qty": 1}])
        more = build_quote(self.catalog, [{"sku": "widget-a", "qty": 2}])
        other_customer = build_quote(self.catalog, [{"sku": "widget-a", "qty": 1}], customer="Acme")
        self.assertNotEqual(base.quote_id, more.quote_id)
        self.assertNotEqual(base.quote_id, other_customer.quote_id)

    def test_duplicate_skus_are_merged(self):
        quote = build_quote(
            self.catalog,
            [{"sku": "widget-a", "qty": 1}, {"sku": "widget-a", "qty": 2}],
        )
        self.assertEqual(len(quote.lines), 1)
        self.assertEqual(quote.lines[0].qty, 3)


class MoneyStringTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_cents(0), "0.00")
        self.assertEqual(format_cents(5), "0.05")
        self.assertEqual(format_cents(3299), "32.99")
        self.assertEqual(format_cents(-199), "-1.99")

    def test_parse(self):
        self.assertEqual(parse_cents("32.99"), 3299)
        self.assertEqual(parse_cents("32.9"), 3290)
        self.assertEqual(parse_cents("32"), 3200)
        self.assertEqual(parse_cents("0.00"), 0)
        self.assertEqual(parse_cents("-1.99"), -199)

    def test_parse_rejects_ambiguity(self):
        for bad in ("32.999", "12,34", "", "abc", 12.34, None):
            with self.subTest(value=bad), self.assertRaises(QuoteError):
                parse_cents(bad)


class CatalogValidationTests(unittest.TestCase):
    def test_valid_catalog(self):
        catalog = Catalog.from_dict(CATALOG)
        self.assertEqual(len(catalog), 4)
        self.assertEqual(catalog.get("widget-a").tax_rate_bps, 825)
        self.assertIn("widget-b", catalog)
        self.assertEqual(catalog.currency, "USD")

    def test_bad_products_rejected_with_reason(self):
        cases = [
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": -1}]}, "unit_price_cents"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": 1.5}]}, "integer"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": True}]}, "integer"),
            ({"products": [{"id": "a", "unit_price_cents": 1}]}, "name"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": 1, "tax_rate_bps": 10001}]}, "tax_rate_bps"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": 1, "currency": "usd"}]}, "ISO-4217"),
            ({"products": []}, "at least one"),
            ({"products": "nope"}, "must be a list"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": 1},
                           {"id": "a", "name": "B", "unit_price_cents": 2}]}, "duplicate"),
            ({"products": [{"id": "a", "name": "A", "unit_price_cents": 1, "currency": "USD"},
                           {"id": "b", "name": "B", "unit_price_cents": 2, "currency": "EUR"}]}, "does not match"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(CatalogError) as ctx:
                    Catalog.from_dict(payload)
                self.assertIn(expected, str(ctx.exception))


class InvoiceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_dict(CATALOG)
        self.quote = build_quote(self.catalog, [{"sku": "widget-a", "qty": 2}], customer="Acme <Corp>")

    def test_json_totals_match_quote(self):
        invoice = render_json(self.quote)
        self.assertEqual(invoice["invoice_id"], invoice_id(self.quote))
        self.assertEqual(invoice["total_cents"], self.quote.total_cents)
        self.assertEqual(invoice["total"], "43.28")  # 2*19.99 = 39.98 + 3.30 tax
        self.assertEqual(invoice["subtotal_cents"], 3998)
        self.assertEqual(invoice["tax_cents"], 330)
        self.assertNotIn("issued_at", invoice)

    def test_invoice_id_is_deterministic(self):
        self.assertEqual(invoice_id(self.quote), invoice_id(build_quote(self.catalog, [("widget-a", 2)], customer="Acme <Corp>")))

    def test_html_escapes_customer_and_contains_totals(self):
        html = render_html(self.quote)
        self.assertIn("&lt;Corp&gt;", html)
        self.assertNotIn("Acme <Corp>", html)
        self.assertIn("USD 43.28", html)
        self.assertIn("INV-", html)
        self.assertTrue(html.lstrip().startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
