#!/usr/bin/env python3
"""End-to-end demo, fully offline: request -> quote -> invoice -> order -> capture -> ledger.

Run from the repository root::

    python3 demo.py

Exit code 0 means every step completed and the ledger reconciled.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from acagent import Catalog, CommerceAgent, Ledger, MockPayPal
from acagent.quote import format_cents

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / ".demo-ledger.jsonl"
OUT_DIR = ROOT / "demo-output"


def rule(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    # Fresh state so the demo can be re-run and read top to bottom.
    if LEDGER_PATH.exists():
        LEDGER_PATH.unlink()
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    catalog = Catalog.load(ROOT / "examples" / "catalog.json")
    payments = MockPayPal(webhook_secret="demo-secret")
    ledger = Ledger(LEDGER_PATH)
    agent = CommerceAgent(catalog, payments=payments, ledger=ledger)

    request_text = "2x widget-a and 1x widget-b for Acme Corp"

    print("=" * 68)
    print("AGENTIC COMMERCE — OFFLINE DEMO")
    print("PayPal client: MockPayPal (in-memory, no credentials, no network)")
    print("=" * 68)

    rule("1. ORDER REQUEST")
    print(f'  "{request_text}"')

    result = agent.create(request_text)

    rule("2. CATALOG RESOLUTION")
    for line in result.quote.lines:
        print(
            f"  {line.sku:<12} {line.name:<12} {line.qty} x "
            f"{format_cents(line.unit_price_cents):>8} = "
            f"{format_cents(line.subtotal_cents):>8}   "
            f"tax {line.tax_rate_bps / 100:.2f}% = {format_cents(line.tax_cents):>6}"
        )

    rule("3. QUOTE")
    q = result.quote
    print(f"  quote id   {q.quote_id}")
    print(f"  customer   {result.customer}")
    print(f"  subtotal   {q.currency} {format_cents(q.subtotal_cents)}")
    print(f"  tax        {q.currency} {format_cents(q.tax_cents)}")
    print(f"  total      {q.currency} {format_cents(q.total_cents)}   ({q.total_cents} integer cents)")

    rule("4. INVOICE")
    invoice_path = OUT_DIR / "invoice.html"
    invoice_path.write_text(result.invoice_html, encoding="utf-8")
    print(f"  invoice id {result.invoice['invoice_id']}")
    print(f"  total      {result.invoice['currency']} {result.invoice['total']}")
    print(f"  printable  {invoice_path.relative_to(ROOT)}")

    rule("5. PAYPAL ORDER (Orders v2, intent CAPTURE)")
    print(f"  order id   {result.order_id}")
    print(f"  status     {result.order.get('status')}")
    print(f"  approve    {result.approval_url}")
    print(f"  idempotency  PayPal-Request-Id = {q.quote_id} (the quote id)")

    rule("6. BUYER APPROVAL")
    agent.approve(result)
    payer = result.order.get("payer", {})
    print(f"  buyer      {payer.get('email_address', 'unknown')} approved in the PayPal window")
    print(f"  status     {result.order.get('status')}")

    rule("7. CAPTURE")
    agent.complete(result)
    captured = ledger.find_capture(result.order_id)
    gross = (result.capture.get("purchase_units") or [{}])[0].get("payments", {}).get("captures", [{}])[0]
    amount = gross.get("amount", {})
    print(f"  capture id {gross.get('id')}")
    print(f"  status     {gross.get('status')}")
    print(f"  amount     {amount.get('currency_code')} {amount.get('value')}")

    rule("8. RECONCILIATION")
    rec = result.reconciliation
    print(f"  {rec.describe()}")
    print(f"  ledger entry #{rec.entry['seq']}  hash {rec.entry['hash'][:24]}…")

    rule("9. IDEMPOTENCY CHECK (replay the same capture)")
    replay = ledger.reconcile(result.order_id, result.invoice, result.capture)
    print(f"  {replay.describe()}")
    print(f"  ledger entries: {len(ledger)} (still one — the duplicate was refused)")

    rule(f"10. LEDGER CONTENTS ({LEDGER_PATH.name}, append-only JSONL)")
    for line_number, raw in enumerate(ledger.raw_lines(), start=1):
        entry = json.loads(raw)
        print(f"  line {line_number}:")
        print(
            f"    {entry['status']:<11} order={entry['order_id']} "
            f"expected={entry['expected_cents']} captured={entry['captured_cents']} "
            f"{entry['currency']}"
        )
        print(f"    prev_hash={entry['prev_hash'][:16]}…  hash={entry['hash'][:16]}…")

    ok, detail = ledger.verify_chain()
    print(f"  chain verification: {'OK' if ok else 'BROKEN'} — {detail}")

    rule("11. AUDIT TRAIL")
    for index, event in enumerate(result.events, start=1):
        print(event.line(index))

    print()
    print(f"RESULT: {rec.status} — {result.quote.currency} {format_cents(rec.captured_cents or 0)} captured for {result.order_id}")
    print(f"        invoice {result.invoice['invoice_id']}, ledger {LEDGER_PATH.relative_to(ROOT)}")
    print(f"        PayPal API calls made: {len(payments.calls)} (all in memory)")
    print()
    return 0 if rec.ok else 1


if __name__ == "__main__":
    sys.exit(main())
