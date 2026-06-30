"""Session-wide Claude API usage + cost tracking.

Every Claude call in the app (Haiku vision extraction, Sonnet Ask, Opus Assist) reports
its `response.usage` here. The UI reads a snapshot to show how many tokens each model has
consumed this run and a *rough* dollar estimate, so the cost of leaving the monitor running
is visible rather than a surprise on the bill.

Pricing is per-1M-tokens (input, output), from Anthropic's published pricing as of
2026-06 — verify current rates at https://platform.claude.com/docs/en/pricing, or override
via the optional [pricing] table in config.toml. Cache-read tokens bill ~0.1x input and
cache-write (5-min) ~1.25x input; we fold those multipliers into the estimate.

Estimate only — it does not see prompt caching discounts perfectly, batch pricing, or
provider differences, and prices change. Treat it as a ballpark, not a billing source.
"""

from __future__ import annotations

import threading

# ($/1M input, $/1M output). Keys are model ids (and the Haiku dated snapshot we pin).
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}
_CACHE_READ_MULT = 0.1     # cached-prefix reads bill ~0.1x the input rate
_CACHE_WRITE_MULT = 1.25   # 5-minute cache writes bill ~1.25x the input rate

_FIELDS = ("input", "output", "cache_read", "cache_write", "calls")


def _ival(obj, name: str) -> int:
    """Read an int field from an SDK usage object or a dict; 0 if missing/None."""
    val = getattr(obj, name, None)
    if val is None and isinstance(obj, dict):
        val = obj.get(name)
    return int(val or 0)


class UsageTracker:
    """Thread-safe accumulator — call record() from any worker thread."""

    def __init__(self, pricing: dict[str, tuple[float, float]] | None = None) -> None:
        self._lock = threading.Lock()
        self._pricing = dict(_DEFAULT_PRICING)
        if pricing:
            self._pricing.update(pricing)
        self._models: dict[str, dict[str, int]] = {}

    def set_pricing(self, pricing: dict[str, tuple[float, float]]) -> None:
        with self._lock:
            self._pricing.update(pricing)

    def rate(self, model: str) -> tuple[float, float] | None:
        """($/1M input, $/1M output) for a model, or None if we have no price."""
        return self._pricing.get(model)

    def record(self, model: str, usage) -> None:
        """Add one response's token usage. `usage` is response.usage (or a dict)."""
        if usage is None:
            return
        with self._lock:
            m = self._models.setdefault(model, {f: 0 for f in _FIELDS})
            m["input"] += _ival(usage, "input_tokens")
            m["output"] += _ival(usage, "output_tokens")
            m["cache_read"] += _ival(usage, "cache_read_input_tokens")
            m["cache_write"] += _ival(usage, "cache_creation_input_tokens")
            m["calls"] += 1

    def _cost(self, model: str, m: dict[str, int]) -> float:
        in_p, out_p = self._pricing.get(model, (0.0, 0.0))
        return (
            m["input"] * in_p
            + m["cache_write"] * in_p * _CACHE_WRITE_MULT
            + m["cache_read"] * in_p * _CACHE_READ_MULT
            + m["output"] * out_p
        ) / 1_000_000

    def snapshot(self) -> dict:
        """Return {models: [{model, calls, input, output, cache_read, cache_write, cost,
        priced}], total_cost, total_calls}. `priced` is False when we have no rate."""
        with self._lock:
            rows = []
            total_cost = 0.0
            total_calls = 0
            for model, m in sorted(self._models.items()):
                cost = self._cost(model, m)
                total_cost += cost
                total_calls += m["calls"]
                rows.append({
                    "model": model,
                    "calls": m["calls"],
                    "input": m["input"],
                    "output": m["output"],
                    "cache_read": m["cache_read"],
                    "cache_write": m["cache_write"],
                    "cost": cost,
                    "priced": model in self._pricing,
                })
            return {"models": rows, "total_cost": total_cost, "total_calls": total_calls}

    def reset(self) -> None:
        with self._lock:
            self._models.clear()


# Process-wide singleton — import and call TRACKER.record(model, resp.usage) at each
# Claude call site; the UI reads TRACKER.snapshot().
TRACKER = UsageTracker()
