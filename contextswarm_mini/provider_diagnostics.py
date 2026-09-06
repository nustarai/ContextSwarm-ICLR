"""Candidate-independent provider/transport diagnostic classification.

Pi keeps intermediate provider errors in an agent's forensic tail.  The
runtime and the formal-matrix operator must agree on which of those strings
are evidence about the provider rather than about a submitted candidate.
This module deliberately returns a small, stable class label and never
returns the input text (which may contain a secret or a private endpoint).
"""

from __future__ import annotations

from typing import Any


def provider_diagnostic_class(value: Any) -> str | None:
    """Return a bounded provider diagnostic class, or ``None``.

    The overload phrases are intentionally contextual.  A bare word such as
    ``error`` or ``timeout`` is not enough: ordinary Judge candidate feedback
    must not trip an experiment-level provider circuit breaker.  Whitespace is
    collapsed so line-wrapped provider receipts are classified consistently.
    """

    if not isinstance(value, str):
        return None
    text = " ".join(value.casefold().split())
    if not text:
        return None

    if any(
        marker in text
        for marker in (
            "our servers are currently overloaded",
            "servers are currently overloaded",
            "server is currently overloaded",
            "our servers are overloaded",
            "servers are overloaded",
            "server is overloaded",
            "service is overloaded",
            "service unavailable",
            "service is unavailable",
            "temporarily unavailable",
            "error occurred while processing your request",
        )
    ):
        return "provider_overload"

    # Some provider adapters only preserve the short retry suffix.  Require a
    # provider/request context so a candidate's prose saying "try again later"
    # cannot manufacture an infrastructure event.
    if "try again later" in text and any(
        marker in text
        for marker in ("server", "service", "provider", "request", "codex error")
    ):
        return "provider_overload"

    if any(marker in text for marker in ("oauth", "authentication failed", "auth refresh")):
        return "provider_auth"
    if any(marker in text for marker in ("rate limit", "too many requests", "429")):
        return "provider_rate_limit"
    if any(
        marker in text
        for marker in (
            "upstream request failed",
            "upstream connect error",
            "websocket error",
            "websocket",
            "connection reset",
            "connection refused",
            "connection termination",
            "network error",
            "fetch failed",
            "transport failure",
            "transport error",
        )
    ):
        return "provider_transport"
    if any(marker in text for marker in ("request timed out", "request timeout", "timed out")):
        return "provider_timeout"
    return None


def is_provider_diagnostic(value: Any) -> bool:
    """Return whether *value* contains a classified provider diagnostic."""

    return provider_diagnostic_class(value) is not None


__all__ = ["is_provider_diagnostic", "provider_diagnostic_class"]
