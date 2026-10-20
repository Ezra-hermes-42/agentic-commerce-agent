# Architecture

## Components

### `catalog.py` — product data
Loads a JSON catalog (`currency`, `products[]`) and validates every field:
SKU and name non-empty, `unit_price_cents` a non-negative integer (bools
rejected), currency a 3-letter uppercase code, `tax_rate_bps` within 0–10000,
no duplicate SKUs, all products sharing the catalog currency. Any violation
raises `CatalogError` naming the field and index. No coercion, ever.

### `quote.py` — pricing
Pure functions over catalog + line items. Normalises `{sku, qty}` objects,
`(sku, qty)` pairs or `{sku: qty}` maps; merges duplicate SKUs; rejects
non-positive or non-integer quantities; sorts lines by SKU. Computes
`subtotal = unit_price_cents * qty` and `tax = (subtotal * bps + 5000) // 10000`
(half-up) per line, sums both, and hashes the canonical payload (sorted keys,
no whitespace, no timestamp, no id) to produce `qte_<24 hex chars>`. A `Quote`
is frozen; changing anything means building a new one with a new id.

### `invoice.py` — presentation
`render_json(quote)` and `render_html(quote)` derive every number from the
`Quote`. The invoice id is deterministic (`INV-` + the first 12 hex chars of the
quote id), so re-rendering the same quote produces the same document. HTML is
escaped with `html.escape`; CSS is inline; no external assets. `issued_at` is
optional and omitted from JSON by default, keeping output byte-stable.

### `transport.py` — HTTP seam
`Response(status, body, headers)` and `json_request(transport, method, url,
headers, payload)` returning `(status, decoded_body)` for *all* HTTP statuses.
`UrllibTransport` maps `HTTPError` to a `Response` (so PayPal's error body
survives) and `URLError`/timeout to `TransportError`. Tests substitute a fake
with the same `request()` signature.

### `paypal.py` — real client
- **Auth:** `POST /v1/oauth2/token` with HTTP Basic and
  `grant_type=client_credentials`. Token cached with its `expires_in`; refreshed
  when less than 60s remain (clock injectable for tests).
- **Create:** `POST /v2/checkout/orders` with `intent: CAPTURE`, one
  `purchase_unit` whose `amount.value` equals `item_total + tax_total`, line
  `items` carrying SKU/quantity/unit amount, and `PayPal-Request-Id` = quote id.
- **Read:** `GET /v2/checkout/orders/{id}`.
- **Capture:** `POST /v2/checkout/orders/{id}/capture` with
  `PayPal-Request-Id: capture-{order_id}`.
- **Webhooks:** `POST /v1/notifications/verify-webhook-signature` with the five
  transmission headers, `webhook_id` and the parsed event. Success requires
  `verification_status == SUCCESS`.
- Errors become `PayPalError` carrying `status`, `payload` and `.issue`
  (`details[0].issue`), so callers can branch on `ORDER_ALREADY_CAPTURED`
  without string matching.
- Missing credentials raise `MissingCredentials` at construction.

### `mock_paypal.py` — offline twin
Same five methods (`access_token` is implicit), no network, in-memory state.
It honours `PayPal-Request-Id` idempotency, returns Orders v2-shaped payloads
with `links[rel=approve]`, models `CREATED → APPROVED → COMPLETED`, rejects
capture before approval (`422 ORDER_NOT_APPROVED`) and a second capture
(`422 ORDER_ALREADY_CAPTURED`), and deep-copies every response so callers cannot
mutate its state. `simulate_buyer_approval()` stands in for the human.

### `ledger.py` — system of record
One JSON object per line, opened with `"a"`, flushed and fsynced. Each entry
carries `seq`, `ts`, order/invoice/quote ids, capture id, expected and captured
cents, currency, `status`, `reasons`, `prev_hash` and `hash`. The hash is
`sha256(canonical_json(entry without hash))`; `prev_hash` links to the previous
line, with 64 zeros as genesis. On load the chain is re-verified; a broken link
or modified field raises `LedgerError`.

`reconcile(order_id, invoice, capture_response)`:

1. If a `RECONCILED` entry already exists for `order_id` → return
   `DUPLICATE_CAPTURE` referencing the original entry, **append nothing**.
2. Extract `(value, currency, capture_id)` from
   `purchase_units[0].payments.captures[0]` (or a bare capture object).
3. Compare currency (case-insensitive) and integer cents against the invoice;
   missing/unparseable values count as `AMOUNT_MISMATCH`.
4. Append one entry with status `RECONCILED` or `REJECTED` plus reasons, and
   return `ReconcileResult`.

Rejected entries do not block a later correct capture of the same order —
correction is allowed, double-recording is not.

### `agent.py` — orchestration
`parse_request` accepts a dict or free text. The deterministic parser recognises
`2x sku` / `sku x2` / `2 x sku` and an optional trailing `for <customer>`.
With `ACAGENT_LLM=1` it first asks an OpenAI-compatible chat endpoint for
`{customer, items}` (temperature 0, JSON mode); any transport error, HTTP error,
schema problem or unknown SKU discards the LLM result and falls back to regex.
The LLM never prices anything — `build_quote` does.

`create()` → quote → invoice → order → approval link (with `quote_id` as the
idempotency key). `approve()` → simulated approval for the mock, or polling for
the real client. `complete()` → capture (falling back to `get_order` on
`ORDER_ALREADY_CAPTURED`) → `reconcile()` → audit events. `run()` chains them.

## Full transaction sequence

```
buyer text        agent                 catalog/quote      paypal            ledger
   │                │                        │                │                 │
   ├─ request ─────▶│                        │                │                 │
   │                ├─ resolve SKUs ────────▶│                │                 │
   │                │◀──── products ─────────┤                │                 │
   │                ├─ build_quote ─────────▶│                │                 │
   │                │◀──── Quote(id,total) ──┤                │                 │
   │                ├─ render_json/html      │                │                 │
   │                ├─ create_order(quote) ──────────────────▶│                 │
   │                │   PayPal-Request-Id=qte_…                │                 │
   │                │◀──── order + approve link ───────────────┤                 │
   │◀─ approval URL ┤                        │                │                 │
   ├─ approve ──────────────────────────────────────────────▶│                 │
   │                ├─ capture_order(id) ────────────────────▶│                 │
   │                │◀──── capture COMPLETED + amount ────────┤                 │
   │                ├─ reconcile(id, invoice, capture) ───────────────────────▶│
   │                │                                          │  append entry   │
   │                │◀──── RECONCILED + entry hash ────────────────────────────┤
   │◀─ receipt/audit┤                        │                │                 │
```

## Failure modes

| Failure | What happens | Recovery |
| --- | --- | --- |
| Capture succeeds, ledger append fails (disk full, fsync error) | Payment is taken; `reconcile()` raises/returns no entry; no fulfilment signal | Re-run reconciliation for the order id. PayPal capture is idempotent (`capture-{order_id}`) and the ledger has no entry, so the retry records it once. There is no 2-phase commit with PayPal: an operator must notice the error. |
| Ledger append succeeds, process dies before returning | Payment recorded, caller sees a failure | Retry `complete()`; PayPal says `ORDER_ALREADY_CAPTURED`, agent fetches the order, ledger returns `DUPLICATE_CAPTURE`; no second entry. |
| Duplicate `complete()` / replayed webhook | `DUPLICATE_CAPTURE`, nothing appended | None needed; original entry is the record. |
| Captured amount ≠ invoice total | `REJECTED` entry with `AMOUNT_MISMATCH`; agent raises `AgentError`; no fulfilment | Investigate; a later correct capture may be recorded, the rejection remains as evidence. |
| Captured currency ≠ invoice currency | `REJECTED` with `CURRENCY_MISMATCH` | Same as above; no FX conversion by design. |
| Catalog price changed between quote and capture | The capture carries the old order amount; reconciliation compares against the invoice you pass in, so pass the same invoice. A rebuilt quote has a new id and would be a different order. | Persist the invoice (or regenerate from the same request and catalog snapshot) before capture. |
| Buyer never approves | `capture_order` → `422 ORDER_NOT_APPROVED`; `approve()` times out with an `AgentError` naming the approval URL | Approve in the browser, raise `ACAGENT_APPROVAL_TIMEOUT`, re-run. |
| OAuth token expires mid-run | Client refreshes within 60s of expiry; a 401 surfaces as `PayPalError` | Retry; token fetch is cheap. |
| Webhook signature invalid or headers missing | `verify_webhook_signature` returns `False`, no state change | Treat as unauthenticated; do not fulfil. |
| Ledger file tampered/truncated | `LedgerError` on construction (hash or chain mismatch) | Restore from backup; do not "fix" the file in place. |
| Two processes append concurrently | Possible `seq`/`prev_hash` collision or interleaved line; unsupported | Single writer per ledger file. |

## Trust boundaries

- Catalog and order text are untrusted input: validated before pricing.
- PayPal responses are untrusted: amounts are parsed from strings, compared in
  cents, and never trusted to define what was invoiced.
- Webhook bodies are untrusted until PayPal verifies the signature.
- The ledger file is trusted only after chain verification; it is not signed by
  an external key, so it detects accidental or naive tampering, not an attacker
  who can rewrite the whole file.
- The mock client is test infrastructure; it must never be selected in
  production. `default_payments()` only picks the real client when both
  credentials are set, and `demo.py` constructs the mock explicitly.
