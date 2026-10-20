"""Append-only JSONL ledger and capture reconciliation.

The ledger is the system of record: every capture attempt that reaches
reconciliation is appended as one JSON line, and each line carries the hash of
the line before it. Nothing is ever rewritten or deleted. If an amount does not
match the invoice, the attempt is recorded as ``REJECTED`` (evidence of the
discrepancy) and the caller is told why. If the same order is captured twice,
the second attempt is *not* appended at all — double-recording a payment is
exactly the failure this component exists to prevent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .paypal import captured_amount_from_response
from .quote import Quote, canonical_json, format_cents, parse_cents

GENESIS_HASH = "0" * 64
ENTRY_TYPE = "capture_reconciliation"

# Reconciliation outcomes.
RECONCILED = "RECONCILED"
REJECTED = "REJECTED"
DUPLICATE_CAPTURE = "DUPLICATE_CAPTURE"

AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


class LedgerError(RuntimeError):
    """The ledger file is unreadable or its hash chain is broken."""


@dataclass
class ReconcileResult:
    status: str
    ok: bool
    order_id: str
    invoice_id: str | None
    quote_id: str | None
    expected_cents: int | None
    captured_cents: int | None
    currency: str | None
    capture_id: str | None
    reasons: list[str] = field(default_factory=list)
    entry: dict | None = None

    def describe(self) -> str:
        if self.status == DUPLICATE_CAPTURE:
            return (
                f"DUPLICATE_CAPTURE for order {self.order_id}: payment already reconciled "
                f"in ledger entry #{self.entry.get('seq') if self.entry else '?'}; not recorded again"
            )
        if self.ok:
            return (
                f"RECONCILED order {self.order_id}: captured "
                f"{format_cents(self.captured_cents or 0)} {self.currency} "
                f"matches invoice {self.invoice_id} ({format_cents(self.expected_cents or 0)})"
            )
        detail = ", ".join(self.reasons) or "unknown reason"
        return (
            f"REJECTED order {self.order_id}: captured "
            f"{format_cents(self.captured_cents) if self.captured_cents is not None else '?'} "
            f"{self.currency or '?'} vs invoice "
            f"{format_cents(self.expected_cents) if self.expected_cents is not None else '?'} "
            f"— {detail}"
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "order_id": self.order_id,
            "invoice_id": self.invoice_id,
            "quote_id": self.quote_id,
            "expected_cents": self.expected_cents,
            "captured_cents": self.captured_cents,
            "currency": self.currency,
            "capture_id": self.capture_id,
            "reasons": list(self.reasons),
            "entry_hash": self.entry.get("hash") if self.entry else None,
            "entry_seq": self.entry.get("seq") if self.entry else None,
        }


def _invoice_fields(invoice: Any) -> tuple[str | None, str | None, int | None, str | None]:
    """Accept an invoice dict or a Quote; return (invoice_id, quote_id, total, currency)."""

    if isinstance(invoice, Quote):
        return None, invoice.quote_id, invoice.total_cents, invoice.currency
    if isinstance(invoice, Mapping):
        return (
            invoice.get("invoice_id"),
            invoice.get("quote_id"),
            invoice.get("total_cents"),
            invoice.get("currency"),
        )
    total = getattr(invoice, "total_cents", None)
    return (
        getattr(invoice, "invoice_id", None),
        getattr(invoice, "quote_id", None),
        total,
        getattr(invoice, "currency", None),
    )


class Ledger:
    """Append-only ledger backed by a JSONL file (or an in-memory list if path is None)."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] | None = None):
        self.path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._entries: list[dict] = []
        self._captured_orders: dict[str, dict] = {}
        if self.path is not None and self.path.exists():
            self._load()

    # -- internals ------------------------------------------------------
    def _load(self) -> None:
        assert self.path is not None
        previous = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(
                        f"{self.path}:{line_number} is not valid JSON: {exc}"
                    ) from exc
                if entry.get("prev_hash") != previous:
                    raise LedgerError(
                        f"{self.path}:{line_number} breaks the hash chain "
                        f"(expected prev_hash {previous[:12]}..., got {str(entry.get('prev_hash'))[:12]}...)"
                    )
                body = {k: v for k, v in entry.items() if k != "hash"}
                if self._hash(body) != entry.get("hash"):
                    raise LedgerError(f"{self.path}:{line_number} has been modified (hash mismatch)")
                previous = entry["hash"]
                self._entries.append(entry)
                if entry.get("type") == ENTRY_TYPE and entry.get("status") == RECONCILED:
                    self._captured_orders.setdefault(entry["order_id"], entry)

    @staticmethod
    def _hash(entry_without_hash: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_json(entry_without_hash).encode("utf-8")).hexdigest()

    def _append(self, entry: dict) -> dict:
        body = dict(entry)
        body["prev_hash"] = self._entries[-1]["hash"] if self._entries else GENESIS_HASH
        body["hash"] = self._hash(body)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        self._entries.append(body)
        return copy.deepcopy(body)

    # -- public API -----------------------------------------------------
    def reconcile(
        self,
        order_id: str,
        invoice: Any,
        capture_response: Mapping[str, Any],
    ) -> ReconcileResult:
        """Compare a PayPal capture against an invoice, append the outcome.

        Order of checks matters:
        1. duplicate capture for this order  -> reject, append nothing
        2. missing/malformed capture amount  -> AMOUNT_MISMATCH
        3. currency differs                  -> CURRENCY_MISMATCH
        4. cents differ                      -> AMOUNT_MISMATCH
        """

        invoice_id, quote_id, expected_cents, invoice_currency = _invoice_fields(invoice)
        value, currency, capture_id = captured_amount_from_response(capture_response or {})

        existing = self._captured_orders.get(order_id)
        if existing is not None:
            return ReconcileResult(
                status=DUPLICATE_CAPTURE,
                ok=False,
                order_id=order_id,
                invoice_id=invoice_id,
                quote_id=quote_id,
                expected_cents=expected_cents,
                captured_cents=existing.get("captured_cents"),
                currency=existing.get("currency"),
                capture_id=existing.get("capture_id"),
                reasons=[DUPLICATE_CAPTURE],
                entry=existing,
            )

        reasons: list[str] = []
        captured_cents: int | None
        try:
            captured_cents = parse_cents(value) if value is not None else None
        except Exception:  # noqa: BLE001 - any unparseable amount is a mismatch
            captured_cents = None
        if captured_cents is None:
            reasons.append(AMOUNT_MISMATCH)
        if not currency:
            reasons.append(CURRENCY_MISMATCH)

        expected_currency = (invoice_currency or "").upper()
        captured_currency = (currency or "").upper()
        if currency and expected_currency and captured_currency != expected_currency:
            reasons.append(CURRENCY_MISMATCH)
        if captured_cents is not None and captured_cents != expected_cents:
            reasons.append(AMOUNT_MISMATCH)
        if expected_cents is None:
            reasons.append(AMOUNT_MISMATCH)

        # Preserve order, drop duplicates (a missing amount can trip two checks).
        reasons = list(dict.fromkeys(reasons))
        ok = not reasons
        status = RECONCILED if ok else REJECTED

        timestamp = datetime.fromtimestamp(self.clock(), timezone.utc)
        entry = self._append(
            {
                "type": ENTRY_TYPE,
                "seq": len(self._entries) + 1,
                "ts": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "order_id": order_id,
                "invoice_id": invoice_id,
                "quote_id": quote_id,
                "capture_id": capture_id,
                "expected_cents": expected_cents,
                "captured_cents": captured_cents,
                "currency": captured_currency or expected_currency or None,
                "status": status,
                "reasons": reasons,
            }
        )
        if ok:
            self._captured_orders[order_id] = entry
        return ReconcileResult(
            status=status,
            ok=ok,
            order_id=order_id,
            invoice_id=invoice_id,
            quote_id=quote_id,
            expected_cents=expected_cents,
            captured_cents=captured_cents,
            currency=captured_currency or expected_currency or None,
            capture_id=capture_id,
            reasons=reasons,
            entry=entry,
        )

    def entries(self) -> list[dict]:
        return copy.deepcopy(self._entries)

    def raw_lines(self) -> list[str]:
        if self.path is not None and self.path.exists():
            return [
                line
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return [json.dumps(entry, sort_keys=True, separators=(",", ":")) for entry in self._entries]

    def find_capture(self, order_id: str) -> dict | None:
        return copy.deepcopy(self._captured_orders.get(order_id))

    def verify_chain(self) -> tuple[bool, str]:
        previous = GENESIS_HASH
        for index, entry in enumerate(self._entries, start=1):
            if entry.get("prev_hash") != previous:
                return False, f"entry #{index} prev_hash mismatch"
            body = {k: v for k, v in entry.items() if k != "hash"}
            if self._hash(body) != entry.get("hash"):
                return False, f"entry #{index} hash mismatch"
            previous = entry["hash"]
        return True, f"{len(self._entries)} entries, chain intact"

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.entries())
