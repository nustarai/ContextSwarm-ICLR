"""Shared policy for optional Agent-proposed Judge validation budgets.

The timeout exposed to a worker is intentionally a small, integer-only
capability.  When supplied, it is the cumulative logical budget for one
validation call, including safe evaluator retries; it is not a transport
deadline or a run-horizon override.  The broker owns validation and the
evaluator applies the final defence-in-depth clamp before constructing Judge
jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


AGENT_TIMEOUT_MIN_SECONDS = 5
AGENT_TIMEOUT_MAX_SECONDS = 300


@dataclass(frozen=True)
class AgentTimeout:
    """The requested value and bounded total budget for one logical call."""

    requested_seconds: int
    effective_seconds: int
    clamped: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "requested_timeout_seconds": self.requested_seconds,
            "effective_timeout_seconds": self.effective_seconds,
            "timeout_clamped": self.clamped,
        }


def normalize_agent_timeout(
    value: Any,
    *,
    configured_timeout_seconds: int | float | None = None,
) -> AgentTimeout:
    """Validate and clamp one worker-proposed timeout.

    ``configured_timeout_seconds`` is the evaluator's own hard ceiling.  It is
    normally 300 seconds, but retaining it here keeps small test adapters and
    deliberately smaller manifests fail-safe.  A malformed value is rejected
    instead of being silently converted; numeric values outside the advertised
    range are clamped and recorded for audit.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timeout_seconds must be an integer")

    cap = AGENT_TIMEOUT_MAX_SECONDS
    if configured_timeout_seconds is not None:
        try:
            configured = float(configured_timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("configured evaluator timeout is invalid") from exc
        if not math.isfinite(configured) or configured <= 0:
            raise ValueError("configured evaluator timeout is invalid")
        cap = min(cap, max(1, int(configured)))

    floor = min(AGENT_TIMEOUT_MIN_SECONDS, cap)
    effective = max(floor, min(cap, int(value)))
    return AgentTimeout(
        requested_seconds=int(value),
        effective_seconds=effective,
        clamped=effective != int(value),
    )


def timeout_fields(timeout: AgentTimeout | None) -> dict[str, Any]:
    """Return bounded, stable metadata suitable for worker/audit records."""

    if timeout is None:
        return {
            "requested_timeout_seconds": None,
            "effective_timeout_seconds": None,
            "timeout_clamped": False,
            "timeout_source": "configured_legacy",
        }
    return {
        **timeout.public_dict(),
        "timeout_source": "agent_requested",
    }


__all__ = [
    "AGENT_TIMEOUT_MAX_SECONDS",
    "AGENT_TIMEOUT_MIN_SECONDS",
    "AgentTimeout",
    "normalize_agent_timeout",
    "timeout_fields",
]
