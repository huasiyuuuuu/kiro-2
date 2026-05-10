"""Close the loop: push stall_guard / stream failures back into the pool.

The problem this solves
-----------------------
stall_guard detects zombies and raises StreamStalled, but the account
that produced the zombie is not told about it. kirogate's default
retry logic may then pick the same bad key and fail again. Over a
5-minute window a single flapping key can chew through several
client requests before anyone notices.

The fix
-------
Give stall_guard (and any other call site that knows a request failed
in an account-attributable way) a tiny `AccountFailureTracker` to
report to. The tracker maintains a per-key sliding window of recent
failures; when the rate crosses a threshold, it bumps that key's
`cooldown_until` so the pool stops picking it.

It's a **policy** sitting on top of the raw bool `pool.accounts[i]`
state — it never deletes or corrupts accounts. When the cooldown
elapses, the key is eligible again automatically.

Design
------
* Pure stdlib + duck-typing against kirogate's Account (only `.key`,
  `.cooldown_until`, `.consecutive_failures`, `.last_error` are read
  or written).
* Deterministic: same inputs (timestamps, fail kinds) produce same
  cooldowns. Helpful for testing.
* Thresholds are env-tunable. Defaults tuned conservatively so one
  bad request doesn't kick a key out.

Integration (2 lines in the stream handler)
-------------------------------------------

    from kirogate_addons.account_health_feedback import AccountFailureTracker

    tracker = AccountFailureTracker(pool)   # init once on startup

    # ... inside the stream error handler ...
    try:
        async for chunk in guarded_stream(...):
            yield chunk
    except StreamStalled as e:
        tracker.report(acct, kind="stall", detail=str(e))
        raise
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            tracker.report(acct, kind="throttle", detail=str(e))
        raise
    else:
        tracker.report_success(acct)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

log = logging.getLogger("kirogate.account_feedback")


@dataclass
class _Window:
    """Per-account sliding-window failure counter."""

    events: Deque[tuple] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = deque()

    def trim(self, cutoff: float) -> None:
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def count(self, kind: Optional[str] = None) -> int:
        if kind is None:
            return len(self.events)
        return sum(1 for _, k, _ in self.events if k == kind)


class AccountFailureTracker:
    """Rolling-window failure tracker that pushes cooldowns back to the pool.

    Parameters
    ----------
    pool :
        kirogate AccountPool (or duck-typed FakePool). We need:
          - .accounts : iterable of objects with .key and .cooldown_until
          - .accounts[i].cooldown_until : mutable float (unix ts)
    window_sec :
        Length of the sliding window. Default 300s (5 min).
    stall_threshold :
        After N 'stall' events in the window, cool the account down.
        Default: 3.
    throttle_threshold :
        After N 'throttle' events (429 / ThrottlingException) in the
        window, cool the account down. Default: 5.
    cooldown_base_sec :
        Initial cooldown on first breach. Default: 60s.
    cooldown_max_sec :
        Maximum cooldown after repeated breaches. Default: 1800s (30min).

    All thresholds are overridable via env vars. See `from_env`.
    """

    def __init__(
        self,
        pool: Any,
        *,
        window_sec: float = 300.0,
        stall_threshold: int = 3,
        throttle_threshold: int = 5,
        cooldown_base_sec: float = 60.0,
        cooldown_max_sec: float = 1800.0,
    ) -> None:
        self._pool = pool
        self._window_sec = window_sec
        self._stall_thr = stall_threshold
        self._throttle_thr = throttle_threshold
        self._cd_base = cooldown_base_sec
        self._cd_max = cooldown_max_sec
        self._lock = threading.Lock()
        self._windows: Dict[str, _Window] = {}
        # Number of times each key has been cooled down (for exponential backoff).
        self._cool_count: Dict[str, int] = {}

    @classmethod
    def from_env(cls, pool: Any, prefix: str = "KIROGATE_FB_") -> "AccountFailureTracker":
        """Build with env-var overrides.

        KIROGATE_FB_WINDOW_SEC, KIROGATE_FB_STALL_THRESHOLD,
        KIROGATE_FB_THROTTLE_THRESHOLD, KIROGATE_FB_COOLDOWN_BASE_SEC,
        KIROGATE_FB_COOLDOWN_MAX_SEC
        """
        def _f(name: str, default: float) -> float:
            v = os.environ.get(prefix + name)
            if v is None:
                return default
            try:
                return float(v)
            except ValueError:
                return default

        return cls(
            pool,
            window_sec=_f("WINDOW_SEC", 300.0),
            stall_threshold=int(_f("STALL_THRESHOLD", 3)),
            throttle_threshold=int(_f("THROTTLE_THRESHOLD", 5)),
            cooldown_base_sec=_f("COOLDOWN_BASE_SEC", 60.0),
            cooldown_max_sec=_f("COOLDOWN_MAX_SEC", 1800.0),
        )

    # ---------------- public reporting API

    def report(
        self,
        account: Any,
        *,
        kind: str = "stall",
        detail: Optional[str] = None,
    ) -> Optional[float]:
        """Record an account-attributable failure. Return the new cooldown
        (or None if threshold not breached).

        `account` is anything with `.key`; we also update `.last_error`
        and `.cooldown_until` on it if present.
        """
        now = time.time()
        key = getattr(account, "key", None)
        if not key:
            return None
        with self._lock:
            w = self._windows.setdefault(key, _Window())
            w.events.append((now, kind, (detail or "")[:120]))
            w.trim(now - self._window_sec)
            # Pick the threshold for this kind.
            threshold = {
                "stall": self._stall_thr,
                "throttle": self._throttle_thr,
            }.get(kind, max(self._stall_thr, self._throttle_thr))
            count = w.count(kind=kind)

        cooldown_until = None
        if count >= threshold:
            cooldown_until = self._apply_cooldown(account, key, kind)

        # Best-effort surface of the error on the account itself so
        # dashboards that read .last_error see a useful message.
        if detail and hasattr(account, "last_error"):
            try:
                account.last_error = f"{kind}: {detail[:140]}"
            except Exception:  # noqa: BLE001
                pass
        if hasattr(account, "consecutive_failures"):
            try:
                account.consecutive_failures = (
                    (account.consecutive_failures or 0) + 1
                )
            except Exception:  # noqa: BLE001
                pass
        return cooldown_until

    def report_success(self, account: Any) -> None:
        """Clear the consecutive-failure counter on a good request.

        We deliberately do NOT clear the sliding window — repeated
        flapping should still accumulate. The window expires naturally.
        """
        if hasattr(account, "consecutive_failures"):
            try:
                account.consecutive_failures = 0
            except Exception:  # noqa: BLE001
                pass
        # Also reset the cool-count so a long-healthy account starts
        # from a fresh base cooldown if it flaps later.
        key = getattr(account, "key", None)
        if key:
            with self._lock:
                self._cool_count.pop(key, None)

    # ---------------- inspection API

    def stats(self) -> Dict[str, Dict[str, Any]]:
        """Return per-account rolling-window stats for /pool/stats."""
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, w in self._windows.items():
                w.trim(now - self._window_sec)
                by_kind: Dict[str, int] = {}
                for _, k, _ in w.events:
                    by_kind[k] = by_kind.get(k, 0) + 1
                out[key] = {
                    "window_sec": self._window_sec,
                    "total_fails": len(w.events),
                    "by_kind": by_kind,
                    "cool_count": self._cool_count.get(key, 0),
                }
        return out

    # ---------------- internals

    def _apply_cooldown(self, account: Any, key: str, kind: str) -> float:
        # Exponential backoff per key.
        self._cool_count[key] = self._cool_count.get(key, 0) + 1
        n = self._cool_count[key]
        cool = min(self._cd_base * (2 ** (n - 1)), self._cd_max)
        now = time.time()
        until = now + cool
        # Don't trample an existing longer cooldown.
        existing = getattr(account, "cooldown_until", 0.0) or 0.0
        new_cd = max(until, existing)
        try:
            account.cooldown_until = new_cd
        except Exception:  # noqa: BLE001
            log.warning("cannot set cooldown_until on account %s", key)
        log.warning(
            "cooling down account key=%s kind=%s for %.0fs (breach #%d)",
            key, kind, cool, n,
        )
        return new_cd


# ---------------------------------------------------------------- selftest


def _selftest() -> None:  # pragma: no cover
    from dataclasses import dataclass

    @dataclass
    class _A:
        key: str
        cooldown_until: float = 0.0
        consecutive_failures: int = 0
        last_error: str = ""

    class _P:
        def __init__(self, accounts):
            self.accounts = accounts

    a = _A(key="k1")
    b = _A(key="k2")
    p = _P([a, b])

    # Default: 3 stalls in 300s -> cooldown kicks in.
    t = AccountFailureTracker(p, window_sec=300, stall_threshold=3,
                               cooldown_base_sec=60, cooldown_max_sec=600)

    assert t.report(a, kind="stall", detail="no bytes for 30s") is None  # 1
    assert a.cooldown_until == 0.0
    assert a.consecutive_failures == 1
    assert "stall" in a.last_error

    assert t.report(a, kind="stall") is None  # 2
    cooldown = t.report(a, kind="stall")  # 3 -> threshold breach
    assert cooldown is not None and cooldown > time.time() + 50, cooldown
    assert a.cooldown_until == cooldown

    # Throttle threshold is separate: 5 events.
    for _ in range(4):
        assert t.report(b, kind="throttle") is None
    c2 = t.report(b, kind="throttle")
    assert c2 is not None and b.cooldown_until == c2

    # Exponential backoff: second breach goes longer.
    t2 = AccountFailureTracker(p, window_sec=300, stall_threshold=1,
                                cooldown_base_sec=10, cooldown_max_sec=100)
    c = _A(key="k3")
    first = t2.report(c, kind="stall")
    second = t2.report(c, kind="stall")
    assert second > first, (first, second)

    # Success clears cool_count.
    t2.report_success(c)
    c.cooldown_until = 0.0
    third = t2.report(c, kind="stall")
    assert third is not None
    assert c.consecutive_failures > 0

    # stats output
    stats = t.stats()
    assert "k1" in stats
    assert stats["k1"]["by_kind"]["stall"] >= 3
    assert stats["k1"]["cool_count"] == 1

    # Don't trample an existing longer cooldown.
    d = _A(key="k4", cooldown_until=time.time() + 99999)
    prev = d.cooldown_until
    t.report(d, kind="stall")
    t.report(d, kind="stall")
    t.report(d, kind="stall")
    assert d.cooldown_until == prev, "existing longer cooldown was overwritten"

    print("account_health_feedback selftest OK")


if __name__ == "__main__":
    _selftest()
