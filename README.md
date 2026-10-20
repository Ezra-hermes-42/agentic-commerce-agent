# agentic-commerce-agent

A working reference implementation of the agentic commerce loop: an agent takes
an order, prices it, renders an invoice, creates and captures a PayPal order, and
reconciles the captured amount against an append-only ledger.

No runtime dependencies. No network required for the demo. Python standard
library only.

```
$ python3 demo.py
...
RESULT: RECONCILED — USD 32.99 captured for 512666D6042542CE5
        invoice INV-3074C48DC0EE, ledger .demo-ledger.jsonl
```

## What it is

Four things, in order, with every step producing a checkable artefact:

1. **Quote** — `2x widget-a and 1x widget-b for Acme Corp` becomes line items
   with integer-cent subtotals, tax and a total, plus a quote id derived from a
   hash of the canonical quote payload.
2. **Invoice** — a JSON document and a one-page printable HTML invoice rendered
   from that quote (never from a second calculation).
3. **PayPal order** — an Orders v2 order with `intent: CAPTURE`, the quote's
   line items as `purchase_units`, and `PayPal-Request-Id` set to the quote id.
4. **Reconciliation** — the capture response is compared to the invoice in
   integer cents; the outcome is appended to a hash-chained JSONL ledger.
   A second capture of the same order is refused, not recorded twice.

## Why agentic commerce

An LLM can already hold a conversation about buying something. What it cannot
safely do is *spend money* unless the surrounding system makes the boring parts
unavoidable: prices that never pass through a float, retries that cannot create
a second charge, a payment that cannot be recorded twice, and an audit record
that a human can inspect after the fact. This repository is the smallest
credible version of that scaffolding, with a real PSP API shape (PayPal Orders
v2) rather than a made-up one.

The agent is deliberately boring: deterministic parsing by default, a two-phase
create/complete flow, and an optional LLM hook that only ever proposes line
items — the catalog, the arithmetic and the payment client stay in charge.

## Architecture

```
              text: "2x widget-a and 1x widget-b for Acme Corp"
                 or {"customer": ..., "items": [...]}
                                  │
                       ┌──────────▼───────────┐
                       │      agent.py        │  parse → resolve → orchestrate
                       │  create() / complete │  audit trail of every step
                       └──────┬────────┬──────┘
                              │        │
                  ┌───────────▼──┐  ┌──▼─────────────────┐
                  │ catalog.py   │  │ quote.py           │
                  │ JSON + SKUs  │  │ integer cents      │
                  │ validation   │  │ half-up tax        │
                  └──────────────┘  │ sha256 → quote_id  │
                                    └──┬─────────────────┘
                                       │ Quote (immutable)
                             ┌─────────▼──────────┐
                             │    invoice.py      │
                             │ JSON + printable   │
                             │ HTML (escaped)     │
                             └─────────┬──────────┘
                                       │
                      ┌────────────────▼─────────────────┐
                      │            paypal.py             │
                      │ OAuth2 token cache, Orders v2,   │
                      │ capture, webhook verification    │
                      │                                  │
                      │  mock_paypal.py — same interface,│
                      │  in-memory, strict state machine │
                      └────────────────┬─────────────────┘
                                       │ capture response
                             ┌─────────▼──────────┐
                             │     ledger.py      │
                             │ reconcile()        │
                             │ append-only JSONL  │
                             │ sha256 hash chain  │
                             └────────────────────┘
```

Both payment clients are driven through `transport.py`, a ~40-line
request/response abstraction. The real client uses `urllib`; tests inject a fake
transport, so request construction and error mapping are covered without a
network.

## Quickstart

Requires Python 3.10+ (developed and tested on 3.14). Nothing to install.

```bash
git clone https://github.com/Ezra-hermes-42/agentic-commerce-agent.git
cd agentic-commerce-agent

python3 demo.py                                     # full offline demo
python3 -m unittest discover -s tests -v            # 55 tests, no network
```

`demo.py` deletes and recreates `.demo-ledger.jsonl` and `demo-output/` so the
narrative is reproducible. It drives the real `CommerceAgent` code path against
`MockPayPal`; only the payment service is fake.

### Sandbox mode

The agent automatically uses the real client when credentials are present:

```bash
export PAYPAL_CLIENT_ID=...
export PAYPAL_CLIENT_SECRET=...
export PAYPAL_WEBHOOK_ID=...          # only needed for verify_webhook_signature()
export PAYPAL_API_BASE=https://api-m.sandbox.paypal.com   # the default

python3 - <<'PY'
from acagent import Catalog, CommerceAgent, Ledger, PayPalClient
catalog = Catalog.load("examples/catalog.json")
agent = CommerceAgent(
    catalog,
    payments=PayPalClient(),
    ledger=Ledger("ledger.jsonl"),
    approval_timeout=180,          # seconds to wait for the buyer to approve
)
result = agent.run("2x widget-a for Acme Corp")
print(result.reconciliation.describe())
PY
```

For sandbox use, the buyer must open `result.approval_url` (also available from
`agent.create(...)`) and approve with a sandbox personal account; `run()` polls
`GET /v2/checkout/orders/{id}` until approved or the timeout expires. To split
the flow, call `agent.create()`, then `agent.approve()`/`agent.complete()` once
the buyer is done.

**The real-client path has never been executed against the live PayPal sandbox.
No credentials exist in the build environment. See Limitations.**

## Layout

| Path | Responsibility |
| --- | --- |
| `acagent/catalog.py` | Load/validate a JSON product catalog; explicit errors |
| `acagent/quote.py` | Deterministic pricing in integer cents; quote id from canonical hash |
| `acagent/invoice.py` | Invoice as JSON and as a minimal printable HTML page |
| `acagent/paypal.py` | Orders v2 client: OAuth2 cache, create/get/capture, webhook verification |
| `acagent/transport.py` | HTTP abstraction (`json_request`) so tests inject a fake |
| `acagent/mock_paypal.py` | In-memory PayPal with the same interface and realistic responses |
| `acagent/ledger.py` | Append-only JSONL ledger, reconciliation, duplicate detection |
| `acagent/agent.py` | Orchestration, deterministic text parsing, optional LLM hook, audit trail |
| `demo.py` | Offline end-to-end demo |
| `tests/` | stdlib `unittest` suites (quote, ledger, webhook, agent flow) |
| `docs/ARCHITECTURE.md` | Component responsibilities, sequence, failure modes |
| `examples/catalog.json` | Sample catalog used by the demo and tests |

## Design decisions

**Integer cents everywhere.** Prices, tax and totals are `int` cents; the only
decimal strings appear at the PayPal boundary (`"32.99"`) and are parsed back
with a strict string parser that rejects floats and >2 decimal places. A
`float` never touches a monetary value.

**Half-up tax rounding, per line.** `(subtotal * bps + 5000) // 10000` gives the
convention invoices expect; Python's `round()` is half-to-even and would
disagree with a human on exact-half cases. Tax is rounded per line and summed,
matching how line-item invoices present tax.

**Quote id = hash of the canonical payload.** Keys sorted, whitespace removed,
lines sorted by SKU, no timestamp, no id. The same items in a different order
produce the same id; a different quantity or customer produces a new one. The
`quote_id` doubles as PayPal's idempotency key, so a retried `create_order`
returns the same order instead of creating a second one.

**Two-phase flow.** `create()` does everything up to and including the approval
link; `complete()` does capture + reconciliation. The real world has a human in
the middle, and a design that pretends otherwise cannot be operated.

**Append-only, hash-chained ledger.** Each line carries `prev_hash` and its own
`sha256` over the canonical entry. The file is opened in append mode and fsynced;
nothing is ever rewritten. `verify_chain()` and the load-time check detect
tampering or truncation. Rejected captures are still recorded — the mismatch is
evidence, and a later correct capture for the same order is allowed.

**Duplicates are refused, not reconciled twice.** `reconcile()` checks for an
existing `RECONCILED` entry for the order before writing. The duplicate attempt
returns `DUPLICATE_CAPTURE` pointing at the original entry and appends nothing.
This is also what catches PayPal's `ORDER_ALREADY_CAPTURED` on a retry: the agent
fetches the order, reconciles the existing capture, and the ledger refuses to
double-record it.

**Webhooks are verified server-side.** `verify_webhook_signature()` posts the
five `PayPal-Transmission-*` headers plus the raw event body to
`/v1/notifications/verify-webhook-signature` and requires
`verification_status == SUCCESS`. Missing headers or a malformed body return
`False` without a network call; a missing `PAYPAL_WEBHOOK_ID` raises rather than
silently accepting.

**The mock is strict.** `MockPayPal` refuses capture before approval and refuses
a second capture with `422 ORDER_ALREADY_CAPTURED`, exactly like the real API.
A permissive fake would hide the bugs the ledger exists to catch.

## Limitations / not yet done

- **No live sandbox transaction was performed.** There are no PayPal credentials
  in the build environment, so `PayPalClient` is covered only by
  fake-transport tests (request shape, headers, token caching, error mapping).
  Treat the real path as unverified until you run it with your own sandbox app.
- No refunds, voids, disputes, partial captures, or multiple captures per order.
- No multi-currency conversion. A catalog has one currency; mismatches are
  rejected, not converted.
- Tax is a flat per-product basis-point rate. No jurisdictions, nexus, exemptions
  or tax-engine integration.
- The ledger is a local JSONL file with no file locking. Two processes writing
  the same ledger concurrently can race on `seq`/`prev_hash`; it is designed for
  a single writer.
- No webhook receiver (HTTP endpoint), queue, or retry worker — verification is
  a library function; event handling is left to the caller.
- No persistence for quotes/invoices beyond regeneration from the catalog and
  request. Changing a catalog price changes future quotes (new quote id) but does
  not rewrite history.
- The optional LLM parser (`ACAGENT_LLM=1`) is off by default and was not
  exercised against a live model endpoint in this build; failures fall back to
  the deterministic regex parser.
- No shipping addresses, customer accounts, authentication, PII handling, or
  rate limiting.
- Invoice HTML is intentionally minimal and not localized.

## AI tools used

- **Hermes Agent** — autonomous build, test execution, and repository setup.
- **Claude / Anthropic models**, accessed through Hermes, for design review and
  code-generation assistance.
- **Python standard library only** — no third-party runtime or test packages.

## License

MIT © 2026 Ezra Labs (Pty) Ltd. See [LICENSE](LICENSE).
