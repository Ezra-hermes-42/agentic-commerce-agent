"""Invoice rendering.

An invoice is a presentational view of a quote, never a separate source of
truth: numbers come straight from the immutable :class:`~acagent.quote.Quote`,
so an invoice can always be regenerated and compared byte-for-byte.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from .quote import Quote, format_cents


def invoice_id(quote: Quote) -> str:
    """Deterministic invoice number derived from the quote id."""

    return "INV-" + quote.quote_id.removeprefix("qte_")[:12].upper()


def render_json(
    quote: Quote,
    *,
    customer: str | None = None,
    issued_at: str | datetime | None = None,
) -> dict:
    """Machine-readable invoice. No timestamp unless explicitly supplied."""

    if isinstance(issued_at, datetime):
        stamp = issued_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    else:
        stamp = issued_at

    invoice: dict[str, Any] = {
        "invoice_id": invoice_id(quote),
        "quote_id": quote.quote_id,
        "customer": customer if customer is not None else quote.customer,
        "currency": quote.currency,
        "lines": [
            {
                "sku": line.sku,
                "name": line.name,
                "qty": line.qty,
                "unit_price_cents": line.unit_price_cents,
                "unit_price": format_cents(line.unit_price_cents),
                "tax_rate_bps": line.tax_rate_bps,
                "subtotal_cents": line.subtotal_cents,
                "tax_cents": line.tax_cents,
                "line_total_cents": line.total_cents,
            }
            for line in quote.lines
        ],
        "subtotal_cents": quote.subtotal_cents,
        "tax_cents": quote.tax_cents,
        "total_cents": quote.total_cents,
        "total": format_cents(quote.total_cents),
    }
    if stamp:
        invoice["issued_at"] = stamp
    return invoice


def render_html(
    quote: Quote,
    *,
    customer: str | None = None,
    issued_at: str | datetime | None = None,
) -> str:
    """A small, printable HTML invoice (one page, no external assets)."""

    invoice = render_json(quote, customer=customer, issued_at=issued_at)
    who = invoice.get("customer") or "Customer"
    rows = []
    for line in invoice["lines"]:
        rows.append(
            "      <tr>"
            f"<td>{html.escape(line['sku'])}</td>"
            f"<td class=\"name\">{html.escape(line['name'])}</td>"
            f"<td class=\"num\">{line['qty']}</td>"
            f"<td class=\"num\">{invoice['currency']} {line['unit_price']}</td>"
            f"<td class=\"num\">{invoice['currency']} {format_cents(line['subtotal_cents'])}</td>"
            f"<td class=\"num\">{line['tax_rate_bps'] / 100:.2f}%</td>"
            f"<td class=\"num\">{invoice['currency']} {format_cents(line['tax_cents'])}</td>"
            "</tr>"
        )
    issued = f"<p class=\"muted\">Issued: {html.escape(str(invoice['issued_at']))}</p>" if invoice.get("issued_at") else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice {html.escape(invoice['invoice_id'])}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1a1a1a; max-width: 820px; margin: 40px auto; padding: 0 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: 0.02em; }}
  .muted {{ color: #666; font-size: 12px; margin: 2px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 28px; font-size: 13px; }}
  th {{ text-align: left; border-bottom: 1px solid #bbb; padding: 8px 6px;
        font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }}
  td {{ padding: 8px 6px; border-bottom: 1px solid #eee; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .name {{ color: #333; }}
  tfoot td {{ border: none; padding: 4px 6px; }}
  tfoot tr.total td {{ border-top: 1px solid #bbb; font-weight: 600; font-size: 15px; }}
  footer {{ margin-top: 32px; font-size: 11px; color: #777; }}
  @media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>
  <h1>Invoice {html.escape(invoice['invoice_id'])}</h1>
  <p class="muted">Quote {html.escape(invoice['quote_id'])} &middot; {html.escape(who)}</p>
  {issued}
  <table>
    <thead>
      <tr><th>SKU</th><th>Description</th><th class="num">Qty</th><th class="num">Unit</th>
          <th class="num">Subtotal</th><th class="num">Tax</th><th class="num">Tax amount</th></tr>
    </thead>
    <tbody>
{chr(10).join(rows)}
    </tbody>
    <tfoot>
      <tr><td colspan="5"></td><td class="num">Subtotal</td>
          <td class="num">{html.escape(invoice['currency'])} {format_cents(invoice['subtotal_cents'])}</td></tr>
      <tr><td colspan="5"></td><td class="num">Tax</td>
          <td class="num">{html.escape(invoice['currency'])} {format_cents(invoice['tax_cents'])}</td></tr>
      <tr class="total"><td colspan="5"></td><td class="num">Total</td>
          <td class="num">{html.escape(invoice['currency'])} {format_cents(invoice['total_cents'])}</td></tr>
    </tfoot>
  </table>
  <footer>Generated by agentic-commerce-agent. Amounts are stored as integer cents;
  this document is a rendering of quote {html.escape(invoice['quote_id'])}.</footer>
</body>
</html>
"""
