"""Separate upstream numeric budget bookkeeping from measured monetary cost."""
from __future__ import annotations

import math
from typing import Any, Iterable


def monetary_cost(extra: dict[str, Any]) -> float | None:
    value = extra.get("reported_cost", extra.get("cost"))
    if (extra.get("cost_unknown") is True or type(value) not in (int, float)
            or not math.isfinite(value) or value < 0):
        return None
    return float(value)


def summarize_cost(messages: Iterable[dict[str, Any]], *, request_count: int | None = None,
                   unknown_requests: int = 0) -> dict[str, Any]:
    """Count canonical provider responses, including format-error observations.

    A missing response has no fabricated cost entry. Completeness requires a
    known price for each recorded response and no known unaccounted requests.
    """
    known, unknown, subtotal = 0, 0, 0.0
    for message in messages:
        extra = message.get("extra")
        if (message.get("role") not in {"assistant", "user"} or not isinstance(extra, dict)
                or "response" not in extra):
            continue
        value = monetary_cost(extra)
        if value is None:
            unknown += 1
        else:
            known += 1
            subtotal += value
    missing = max(unknown_requests, max(0, request_count - known - unknown) if request_count is not None else 0)
    complete = bool(known) and not unknown and not missing
    return {"currency": "USD", "reported_total": subtotal if complete else None,
            "known_subtotal": subtotal, "responses_with_known_cost": known,
            "responses_with_unknown_cost": unknown, "requests_without_cost_response": missing,
            "complete": complete,
            "cost_limit_scope": "known_costs_only; unknown costs are not bounded by the monetary limit"}
