"""End-to-end agent flow against the in-memory PayPal fake (offline, no network)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acagent import AgentError, Catalog, CommerceAgent, Ledger, MockPayPal
from acagent.ledger import DUPLICATE_CAPTURE, RECONCILED
from acagent.paypal import PayPalError

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "2x widget-a and 1x widget-b for Acme Corp"


class AgentFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.jsonl"
        self.catalog = Catalog.load(ROOT / "examples" / "catalog.json")
        self.payments = MockPayPal()
        self.ledger = Ledger(self.path)
        # Keep the deterministic parser path even if the host has LLM env vars set.
        patcher = mock.patch.dict(os.environ, {"ACAGENT_LLM": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = CommerceAgent(self.catalog, payments=self.payments, ledger=self.ledger)

    def test_full_flow_quote_invoice_capture_reconcile(self):
        result = self.agent.run(REQUEST)

        self.assertEqual(result.customer, "Acme Corp")
        self.assertEqual(len(result.quote.lines), 2)
        self.assertEqual(result.quote.subtotal_cents, 3048)
        self.assertEqual(result.quote.tax_cents, 251)
        self.assertEqual(result.quote.total_cents, 3299)

        self.assertTrue(result.invoice["invoice_id"].startswith("INV-"))
        self.assertEqual(result.invoice["total_cents"], 3299)

        self.assertTrue(result.order_id)
        self.assertIn("checkoutnow?token=", result.approval_url or "")
        self.assertEqual(result.order["intent"], "CAPTURE")
        self.assertEqual(result.order["purchase_units"][0]["amount"]["value"], "32.99")

        self.assertEqual(result.reconciliation.status, RECONCILED)
        self.assertTrue(result.reconciliation.ok)
        self.assertEqual(result.reconciliation.captured_cents, 3299)
        self.assertIsNotNone(result.capture_id)

        self.assertEqual(len(self.ledger), 1)
        self.assertEqual(self.ledger.entries()[0]["order_id"], result.order_id)
        self.assertTrue(self.ledger.verify_chain()[0])

        steps = [event.step for event in result.events]
        self.assertEqual(
            steps,
            ["request", "quote", "invoice", "paypal_order", "buyer_approval", "capture", "reconcile", "ledger"],
        )

        # The fake was actually driven in the right order.
        methods = [call["method"] for call in self.payments.calls]
        self.assertEqual(methods[:4], ["POST", "BUYER_APPROVED", "POST", "CAPTURE"])

    def test_replaying_a_capture_is_refused_not_double_recorded(self):
        result = self.agent.run(REQUEST)
        replay = self.ledger.reconcile(result.order_id, result.invoice, result.capture)
        self.assertEqual(replay.status, DUPLICATE_CAPTURE)
        self.assertFalse(replay.ok)
        self.assertEqual(len(self.ledger), 1)

        # Persisted duplicate protection survives a restart.
        reloaded = Ledger(self.path)
        again = reloaded.reconcile(result.order_id, result.invoice, result.capture)
        self.assertEqual(again.status, DUPLICATE_CAPTURE)
        self.assertEqual(len(reloaded), 1)

    def test_second_run_of_same_order_hits_paypal_already_captured_then_dedupes(self):
        first = self.agent.run(REQUEST)
        second = self.agent.run(REQUEST)  # same quote -> same PayPal-Request-Id -> same order
        self.assertEqual(first.order_id, second.order_id)
        self.assertEqual(len(self.payments.orders), 1)
        self.assertEqual(second.reconciliation.status, DUPLICATE_CAPTURE)
        self.assertEqual(len(self.ledger), 1)
        self.assertIn("capture", [event.step for event in second.events])

    def test_structured_request(self):
        result = self.agent.run({"customer": "Globex", "items": [{"sku": "gizmo-pro", "qty": 1}]})
        self.assertEqual(result.customer, "Globex")
        self.assertEqual(result.quote.total_cents, 24900 + 2054)  # 8.25% of 24900 = 2054.25 -> 2054
        self.assertEqual(result.reconciliation.status, RECONCILED)

    def test_capture_before_approval_is_refused(self):
        result = self.agent.create(REQUEST)
        with self.assertRaises(PayPalError) as ctx:
            self.payments.capture_order(result.order_id)
        self.assertEqual(ctx.exception.issue, "ORDER_NOT_APPROVED")
        self.assertEqual(len(self.ledger), 0)

    def test_create_is_idempotent_for_the_same_quote(self):
        one = self.agent.create(REQUEST)
        two = self.agent.create(REQUEST)
        self.assertEqual(one.quote.quote_id, two.quote.quote_id)
        self.assertEqual(one.order_id, two.order_id)
        self.assertEqual(len(self.payments.orders), 1)

    def test_unknown_sku_is_rejected_before_any_payment_call(self):
        with self.assertRaises(AgentError) as ctx:
            self.agent.create("2x does-not-exist for Acme")
        self.assertIn("unknown sku", str(ctx.exception))
        self.assertEqual(self.payments.calls, [])
        self.assertEqual(len(self.ledger), 0)

    def test_unparseable_request(self):
        with self.assertRaises(AgentError) as ctx:
            self.agent.create("please send me something nice")
        self.assertIn("could not find any line items", str(ctx.exception))

    def test_parse_text_without_customer(self):
        parsed = self.agent.parse_request("3x widget-a")
        self.assertIsNone(parsed["customer"])
        self.assertEqual(parsed["items"], [{"sku": "widget-a", "qty": 3}])

    def test_parse_text_sku_first_form(self):
        parsed = self.agent.parse_request("widget-a x2, widget-b x1 for Initech")
        self.assertEqual(parsed["customer"], "Initech")
        self.assertEqual(
            sorted((item["sku"], item["qty"]) for item in parsed["items"]),
            [("widget-a", 2), ("widget-b", 1)],
        )

    def test_invoice_html_is_rendered(self):
        result = self.agent.create(REQUEST)
        self.assertIn("<!doctype html>", result.invoice_html)
        self.assertIn("Acme Corp", result.invoice_html)

    def test_audit_trail_can_be_printed(self):
        result = self.agent.run(REQUEST)
        buffer = io.StringIO()
        self.agent.print_audit(result, buffer)
        output = buffer.getvalue()
        self.assertIn("AUDIT TRAIL", output)
        self.assertIn("paypal_order", output)
        self.assertIn("RECONCILED", output)

    def test_mock_webhook_verification(self):
        body = '{"event_type":"CHECKOUT.ORDER.APPROVED"}'
        self.assertTrue(self.payments.verify_webhook_signature({"paypal-transmission-sig": "mock-webhook-secret"}, body))
        self.assertFalse(self.payments.verify_webhook_signature({"paypal-transmission-sig": "forged"}, body))


if __name__ == "__main__":
    unittest.main()
