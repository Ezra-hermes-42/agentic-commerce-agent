"""Ledger tests: happy-path reconciliation, mismatches, duplicates, hash chain."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from acagent import Catalog, Ledger, build_quote, render_json
from acagent.ledger import (
    AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    DUPLICATE_CAPTURE,
    GENESIS_HASH,
    RECONCILED,
    REJECTED,
    LedgerError,
)

CATALOG = Catalog.from_dict(
    {
        "currency": "USD",
        "products": [{"id": "widget-a", "name": "Widget A", "unit_price_cents": 1299, "tax_rate_bps": 825}],
    }
)


def invoice_for(qty: int = 2) -> dict:
    quote = build_quote(CATALOG, [{"sku": "widget-a", "qty": qty}], customer="Acme")
    return render_json(quote)


def capture_for(value: str, currency: str = "USD", capture_id: str = "CAP-1", order_id: str = "ORDER-1") -> dict:
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "reference_id": "qte_x",
                "amount": {"currency_code": currency, "value": value},
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {"currency_code": currency, "value": value},
                        }
                    ]
                },
            }
        ],
    }


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.jsonl"
        self.ledger = Ledger(self.path, clock=lambda: 1_700_000_000.0)
        self.invoice = invoice_for(2)  # 25.98 + 2.14 tax = 28.12


class HappyPathTests(LedgerTestCase):
    def test_reconcile_records_entry_and_hash(self):
        result = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, RECONCILED)
        self.assertEqual(result.expected_cents, 2812)
        self.assertEqual(result.captured_cents, 2812)
        self.assertEqual(result.reasons, [])
        self.assertEqual(result.capture_id, "CAP-1")

        entries = self.ledger.entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["prev_hash"], GENESIS_HASH)
        self.assertEqual(len(entry["hash"]), 64)
        self.assertEqual(entry["status"], RECONCILED)
        self.assertEqual(entry["ts"], "2023-11-14T22:13:20.000Z")

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), entry)

    def test_chain_verifies_and_survives_reload(self):
        self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.ledger.reconcile("ORDER-2", self.invoice, capture_for("28.12", capture_id="CAP-2", order_id="ORDER-2"))
        ok, detail = self.ledger.verify_chain()
        self.assertTrue(ok, detail)

        reloaded = Ledger(self.path)
        self.assertEqual(len(reloaded), 2)
        self.assertEqual(reloaded.entries()[1]["prev_hash"], reloaded.entries()[0]["hash"])
        self.assertTrue(reloaded.verify_chain()[0])
        self.assertIsNotNone(reloaded.find_capture("ORDER-1"))

    def test_bare_capture_object_is_accepted(self):
        result = self.ledger.reconcile(
            "ORDER-1",
            self.invoice,
            {"id": "CAP-9", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "28.12"}},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.capture_id, "CAP-9")


class MismatchTests(LedgerTestCase):
    def test_amount_mismatch_is_rejected_and_recorded(self):
        result = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("27.12"))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, REJECTED)
        self.assertIn(AMOUNT_MISMATCH, result.reasons)
        self.assertEqual(result.expected_cents, 2812)
        self.assertEqual(result.captured_cents, 2712)

        entries = self.ledger.entries()
        self.assertEqual(len(entries), 1, "a rejected capture must still be auditable")
        self.assertEqual(entries[0]["status"], REJECTED)
        self.assertIn(AMOUNT_MISMATCH, entries[0]["reasons"])
        self.assertIsNone(self.ledger.find_capture("ORDER-1"))

    def test_currency_mismatch_is_rejected(self):
        result = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12", currency="EUR"))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, REJECTED)
        self.assertIn(CURRENCY_MISMATCH, result.reasons)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.entry["currency"], "EUR")

    def test_missing_or_malformed_amount_is_rejected(self):
        for payload in ({}, {"purchase_units": [{"payments": {"captures": []}}]},
                        capture_for("28.1.2")):
            with self.subTest(payload=payload):
                result = self.ledger.reconcile("ORDER-X", self.invoice, payload)
                self.assertFalse(result.ok)
                self.assertIn(AMOUNT_MISMATCH, result.reasons)

    def test_retry_after_mismatch_is_allowed(self):
        first = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("1.00"))
        self.assertEqual(first.status, REJECTED)
        second = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.assertTrue(second.ok)
        self.assertEqual(len(self.ledger), 2)
        self.assertEqual(second.entry["seq"], 2)


class DuplicateTests(LedgerTestCase):
    def test_second_reconcile_is_duplicate_and_not_recorded(self):
        first = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.assertTrue(first.ok)
        second = self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.assertFalse(second.ok)
        self.assertEqual(second.status, DUPLICATE_CAPTURE)
        self.assertEqual(second.reasons, [DUPLICATE_CAPTURE])
        self.assertEqual(second.entry["seq"], 1, "duplicate points at the original entry")
        self.assertEqual(len(self.ledger), 1, "duplicate must not append a second entry")
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_duplicate_detected_after_reload(self):
        self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        reloaded = Ledger(self.path)
        duplicate = reloaded.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        self.assertEqual(duplicate.status, DUPLICATE_CAPTURE)
        self.assertEqual(len(reloaded), 1)


class IntegrityTests(LedgerTestCase):
    def test_tampered_entry_is_detected_on_load(self):
        self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw.replace('"captured_cents":2812', '"captured_cents":1'), encoding="utf-8")
        with self.assertRaises(LedgerError) as ctx:
            Ledger(self.path)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_corrupt_line_is_reported(self):
        self.path.write_text("{not json}\n", encoding="utf-8")
        with self.assertRaises(LedgerError) as ctx:
            Ledger(self.path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_append_only_file_only_grows(self):
        self.ledger.reconcile("ORDER-1", self.invoice, capture_for("28.12"))
        first = self.path.read_text(encoding="utf-8")
        self.ledger.reconcile("ORDER-2", self.invoice, capture_for("28.12", capture_id="CAP-2", order_id="ORDER-2"))
        second = self.path.read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first), "existing bytes must never be rewritten")
        self.assertEqual(len(second.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
