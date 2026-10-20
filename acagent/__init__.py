"""agentic-commerce-agent: a deterministic quote -> invoice -> PayPal -> ledger pipeline.

The package is intentionally dependency-free (Python standard library only) so the
whole flow can be exercised offline against :mod:`acagent.mock_paypal`.
"""

from .catalog import Catalog, CatalogError, Product
from .quote import Quote, QuoteError, build_quote
from .invoice import render_html, render_json
from .ledger import Ledger, ReconcileResult
from .paypal import MissingCredentials, PayPalClient, PayPalError
from .mock_paypal import MockPayPal
from .agent import AgentError, CommerceAgent, OrderResult

__all__ = [
    "AgentError",
    "Catalog",
    "CatalogError",
    "CommerceAgent",
    "Ledger",
    "MissingCredentials",
    "MockPayPal",
    "OrderResult",
    "PayPalClient",
    "PayPalError",
    "Product",
    "Quote",
    "QuoteError",
    "ReconcileResult",
    "build_quote",
    "render_html",
    "render_json",
]

__version__ = "0.1.0"
