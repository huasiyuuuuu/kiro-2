"""Observer that tells you whether Kiro's free prefix cache is hitting.

Why this exists
---------------
Kiro's backend does implicit Anthropic-style prefix caching at no
charge (see cache_stability.py for the empirical proof). But it
doesn't expose a `cache_read_input_tokens` field on the wire — the
only signal is `meteringEvent.usage` getting cheaper after the first
call with a given prefix.

This observer tracks that signal over time per (model, prefix_sig) so
you can:
  * verify cache hits are actually happening after the first turn of
    a conversation (warm cost should be ~53% of cold cost),
  * alert if a translator change regresses byte-stability of history
    (hit rate suddenly drops to near zero),
  * compute real cost savings per tenant / per model for billing.

Measured numbers you should expect to see (from live probes against
claude-haiku-4.5 with a 10k-token prefix):

    cold call:  0.01550 credits   -> counted as NOT a hit
    warm call:  0.00826 credits   -> counted as a hit (~47% off)

Usage
-----
    observer = CacheObserver()

    # After each streaming call completes:
    observer.record(
        model="claude-sonnet-4.6",
        prefix_signature=payload_prefix_signature(kiro_payload),
        credits=total_credits_from_metering_events,
    )

    # Expose as an admin endpoint:
    @app.get("/pool/cache")
    async def cache_stats(...):
        return observer.snapshot()

The observer uses a bounded LRU (default 2048 distinct signatures); old
ones evict as new conversations arrive. Stats are cheap to compute on
demand. Wire into Prometheus if you want alerting.

How "hit" is detected
---------------------
We classify a record as a HIT if its cost is <=65% of the MAX cost
ever seen for the same (model, sig). 65% is deliberately conservative:
actual warm hits on haiku are ~53%, but sonnet/opus hits run ~35-70%
depending on output length. Tune via `hit_ratio_threshold`.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class _Entry:
    model: str
    prefix_signature: str
    credits: float
    ts: float


@dataclass
class _PerKeyStats:
    seen: int = 0
    hits: int = 0
    min_cost: float = float("inf")
    max_cost: float = 0.0
    last_cost: float = 0.0
    total_credits: float = 0.0
    total_credits_baseline: float = 0.0  # credits IF every call paid max_cost
    last_seen_at: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        saved = max(self.total_credits_baseline - self.total_credits, 0.0)
        return {
            "seen": self.seen,
            "hits": self.hits,
            "hit_rate": (self.hits / self.seen) if self.seen else 0.0,
            "min_cost": None if self.min_cost == float("inf") else round(self.min_cost, 5),
            "max_cost": round(self.max_cost, 5),
            "last_cost": round(self.last_cost, 5),
            "total_credits": round(self.total_credits, 5),
            "baseline_credits": round(self.total_credits_baseline, 5),
            "credits_saved": round(saved, 5),
            "savings_pct": round(100.0 * saved / self.total_credits_baseline, 1)
                            if self.total_credits_baseline > 0 else 0.0,
            "last_seen_at": self.last_seen_at,
        }


class CacheObserver:
    """Thread-safe counter of cache behaviour per (model, prefix_sig).

    Parameters
    ----------
    window : int
        (Deprecated; kept for backward compat.) Previously this capped
        a per-key rolling buffer, but the buffer was never read — only
        aggregate counters are used. Pass-through for API stability.
    max_keys : int
        Max distinct (model, sig) pairs tracked before LRU eviction.
    hit_ratio_threshold : float
        A call is counted as a cache HIT if its cost is <= this fraction
        of the max cost ever seen for the same key. Default 0.65 is
        conservative (real warm hits on haiku are ~0.53).
    """

    def __init__(
        self,
        *,
        window: int = 50,
        max_keys: int = 2048,
        hit_ratio_threshold: float = 0.65,
    ) -> None:
        self._window = window  # retained for API compat; unused.
        self._max_keys = max_keys
        self._ratio = hit_ratio_threshold
        self._lock = threading.Lock()
        # Old _history deque removed — it filled memory without being read.
        self._stats: Dict[Tuple[str, str], _PerKeyStats] = {}
        self._key_order: Deque[Tuple[str, str]] = deque()

    def record(
        self,
        *,
        model: str,
        prefix_signature: str,
        credits: float,
        now: Optional[float] = None,
    ) -> bool:
        """Record a completed request. Returns True if it counts as a cache hit."""
        now = now or time.time()
        key = (model, prefix_signature)
        with self._lock:
            stats = self._stats.get(key)
            if stats is None:
                if len(self._key_order) >= self._max_keys:
                    # evict oldest
                    evicted = self._key_order.popleft()
                    self._stats.pop(evicted, None)
                stats = _PerKeyStats()
                self._stats[key] = stats
                self._key_order.append(key)

            stats.seen += 1
            stats.last_cost = credits
            stats.last_seen_at = now
            stats.max_cost = max(stats.max_cost, credits)
            stats.min_cost = min(stats.min_cost, credits)
            stats.total_credits += credits

            # Classify: hit iff cost well below this key's max so far.
            is_hit = (
                stats.seen > 1 and stats.max_cost > 0
                and credits <= stats.max_cost * self._ratio
            )
            if is_hit:
                stats.hits += 1

            # Baseline: what we'd pay if every call paid max cost seen.
            stats.total_credits_baseline = stats.max_cost * stats.seen

            return is_hit

    def snapshot(self) -> Dict[str, Any]:
        """Return a json-friendly summary of observed cache behaviour."""
        with self._lock:
            by_model: Dict[str, Dict[str, Any]] = {}
            grand_total = 0.0
            grand_baseline = 0.0
            grand_seen = 0
            grand_hits = 0
            keys_sorted = sorted(
                self._stats.items(),
                key=lambda kv: kv[1].last_seen_at, reverse=True,
            )
            top_keys: List[Dict[str, Any]] = []
            for (model, sig), stats in keys_sorted:
                by = by_model.setdefault(model, {
                    "seen": 0, "hits": 0, "total_credits": 0.0,
                    "baseline_credits": 0.0, "signatures": 0,
                })
                by["seen"] += stats.seen
                by["hits"] += stats.hits
                by["total_credits"] += stats.total_credits
                by["baseline_credits"] += stats.total_credits_baseline
                by["signatures"] += 1
                grand_total += stats.total_credits
                grand_baseline += stats.total_credits_baseline
                grand_seen += stats.seen
                grand_hits += stats.hits
                if len(top_keys) < 10:
                    top_keys.append({
                        "model": model,
                        "prefix_sig": sig,
                        **stats.as_dict(),
                    })
            # finalise per-model
            for model, by in by_model.items():
                by["hit_rate"] = (by["hits"] / by["seen"]) if by["seen"] else 0.0
                by["credits_saved"] = max(
                    by["baseline_credits"] - by["total_credits"], 0.0
                )
                by["savings_pct"] = (
                    100.0 * by["credits_saved"] / by["baseline_credits"]
                    if by["baseline_credits"] > 0 else 0.0
                )
                by["total_credits"] = round(by["total_credits"], 5)
                by["baseline_credits"] = round(by["baseline_credits"], 5)
                by["credits_saved"] = round(by["credits_saved"], 5)
            return {
                "total_seen": grand_seen,
                "total_hits": grand_hits,
                "overall_hit_rate": (grand_hits / grand_seen) if grand_seen else 0.0,
                "total_credits": round(grand_total, 5),
                "baseline_credits": round(grand_baseline, 5),
                "credits_saved": round(max(grand_baseline - grand_total, 0.0), 5),
                "savings_pct": (
                    round(100.0 * max(grand_baseline - grand_total, 0.0)
                          / grand_baseline, 1) if grand_baseline > 0 else 0.0
                ),
                "by_model": by_model,
                "top_signatures": top_keys,
                "distinct_signatures": len(self._stats),
            }

    def prometheus(self) -> str:
        """Render the snapshot as Prometheus text exposition format.

        Plug into kirogate's existing /metrics endpoint, e.g.:

            @app.get("/metrics")
            async def metrics():
                return Response(
                    "\\n".join([
                        my_other_metrics(),
                        app.state.cache_observer.prometheus(),
                    ]),
                    media_type="text/plain",
                )
        """
        snap = self.snapshot()
        lines = []
        lines.append("# HELP kirogate_cache_total_calls Total LLM calls observed.")
        lines.append("# TYPE kirogate_cache_total_calls counter")
        lines.append(f"kirogate_cache_total_calls {snap['total_seen']}")
        lines.append("# HELP kirogate_cache_hits Calls that paid less than max-cost threshold.")
        lines.append("# TYPE kirogate_cache_hits counter")
        lines.append(f"kirogate_cache_hits {snap['total_hits']}")
        lines.append("# HELP kirogate_cache_hit_rate Fraction of calls classified as cache hits.")
        lines.append("# TYPE kirogate_cache_hit_rate gauge")
        lines.append(f"kirogate_cache_hit_rate {snap['overall_hit_rate']:.6f}")
        lines.append("# HELP kirogate_cache_credits_total Total credits actually charged.")
        lines.append("# TYPE kirogate_cache_credits_total counter")
        lines.append(f"kirogate_cache_credits_total {snap['total_credits']:.5f}")
        lines.append("# HELP kirogate_cache_credits_saved Estimated credits saved vs paying max-cost every call.")
        lines.append("# TYPE kirogate_cache_credits_saved counter")
        lines.append(f"kirogate_cache_credits_saved {snap['credits_saved']:.5f}")

        lines.append("# HELP kirogate_cache_by_model_hit_rate Hit rate per model.")
        lines.append("# TYPE kirogate_cache_by_model_hit_rate gauge")
        for model, m in snap.get("by_model", {}).items():
            # Sanitize model name for Prom label.
            mlabel = model.replace('"', '').replace("\\\\", "")
            lines.append(
                f'kirogate_cache_by_model_hit_rate{{model="{mlabel}"}} {m["hit_rate"]:.6f}'
            )
            lines.append(
                f'kirogate_cache_by_model_credits_saved{{model="{mlabel}"}} {m["credits_saved"]:.5f}'
            )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- selftest

def _selftest() -> None:  # pragma: no cover
    obs = CacheObserver(hit_ratio_threshold=0.65)

    # Simulate: same sig, cold=0.0197 then warm=0.0105 five times.
    # This matches the empirical haiku numbers from probe_cache3.
    sig = "deadbeef12345678"
    assert obs.record(model="haiku", prefix_signature=sig, credits=0.0197) is False  # first call — no baseline yet
    for _ in range(5):
        hit = obs.record(model="haiku", prefix_signature=sig, credits=0.0105)
        assert hit is True, "warm call should count as hit"

    snap = obs.snapshot()
    haiku = snap["by_model"]["haiku"]
    assert haiku["seen"] == 6 and haiku["hits"] == 5, haiku
    assert haiku["hit_rate"] > 0.8
    # Saved = 5 * (0.0197 - 0.0105) = 0.046
    assert 0.04 < haiku["credits_saved"] < 0.06, haiku

    # Different sig -> separate bucket
    obs.record(model="haiku", prefix_signature="other", credits=0.015)
    snap2 = obs.snapshot()
    assert snap2["distinct_signatures"] == 2

    # Prometheus export
    prom = obs.prometheus()
    assert "kirogate_cache_total_calls 7" in prom
    assert "kirogate_cache_hits 5" in prom
    assert 'kirogate_cache_by_model_hit_rate{model="haiku"}' in prom

    print("cache_observer selftest OK")
    import json
    print(json.dumps(snap, indent=2))


if __name__ == "__main__":
    _selftest()
