"""Pluggable account-selection strategies for kirogate's AccountPool.

kirogate's stock `AccountPool.pick()` is a healthy-only round-robin with
stickiness. That's fine for balanced load but wastes credits when your
keys have very different remaining balances, and it's perfectly
predictable which Kiro's backend may exploit to detect automation.

This module provides:

  Selector protocol               — abstract picker, takes candidates + now.
  RoundRobinSelector              — same as stock kirogate.
  LeastUsedSelector               — picks the key with fewest total_requests.
  WeightedByCreditsSelector       — picks proportionally to remaining credits
                                    (uses quota_monitor if available; degrades
                                    to round-robin if not).
  WeightedRandomSelector          — weighted random with configurable jitter
                                    (0.0 == deterministic, 1.0 == full random).
  PerAccountSemaphore             — async concurrency limiter keyed by account.
  SelectorChain                   — apply a primary strategy with a fallback.
  make_selector(name, **kwargs)   — factory reading the same env vars as kirogate.

Integration sketch (4 lines in kirogate/pool.py):

    from kirogate_addons.pool_strategies import make_selector, PerAccountSemaphore

    class AccountPool:
        def __init__(self, accounts, cfg=None, selector=None, semaphore=None):
            ...
            self._selector  = selector  or make_selector("round_robin")
            self._semaphore = semaphore or PerAccountSemaphore(limit_per_account=4)

    def pick(self, sticky_key=None):
        ...
        # replace the inline round-robin block with:
        chosen = self._selector.pick(healthy, now=now)

Everything is sync except PerAccountSemaphore which uses asyncio.Semaphore.
That matches the mixed sync/async style kirogate already uses.
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)


# ---------------------------------------------------------------- types


@runtime_checkable
class _AccountLike(Protocol):
    """Structural subset of kirogate.pool.Account we rely on.

    Duck-typed so this module never imports kirogate directly.
    """

    key: str
    name: str
    cooldown_until: float
    total_requests: int
    total_failures: int


RemainingCreditsFn = Callable[[str], Optional[float]]
"""Function: given an account key, return remaining credits or None."""


# ---------------------------------------------------------------- selectors


class Selector(Protocol):
    """Abstract strategy. Pure function of candidates + wallclock."""

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        ...


class RoundRobinSelector:
    """Stateful cursor over the account list.

    Thread-safe; kirogate's pool holds a lock around `pick()` already, but
    we keep our own lock so this class is usable standalone.
    """

    def __init__(self) -> None:
        self._idx = -1
        self._lock = threading.Lock()

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        if not candidates:
            raise ValueError("no candidates")
        with self._lock:
            self._idx = (self._idx + 1) % len(candidates)
            return candidates[self._idx]


class LeastUsedSelector:
    """Pick the account with the smallest total_requests. Tiebreak: least failures."""

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        if not candidates:
            raise ValueError("no candidates")
        return min(
            candidates,
            key=lambda a: (a.total_requests, a.total_failures),
        )


class WeightedByCreditsSelector:
    """Pick proportionally to remaining credits.

    Requires a RemainingCreditsFn (e.g. bound to quota_monitor). If the
    function returns None for a key (quota not yet known), that key gets
    the configured `unknown_weight` (default: average of known weights,
    so unknown keys still get tried).

    If quota info is missing for everyone, falls back to round-robin.
    """

    def __init__(
        self,
        get_remaining: RemainingCreditsFn,
        *,
        unknown_weight: Optional[float] = None,
        min_weight: float = 0.01,
        softmax_temperature: float = 1.0,
    ) -> None:
        self._get_remaining = get_remaining
        self._unknown_weight = unknown_weight
        self._min_weight = min_weight
        self._temperature = max(softmax_temperature, 0.01)
        self._fallback = RoundRobinSelector()
        self._rng = random.Random()

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        if not candidates:
            raise ValueError("no candidates")

        remainings: List[Optional[float]] = [
            self._get_remaining(a.key) for a in candidates
        ]
        known = [r for r in remainings if r is not None]
        if not known:
            return self._fallback.pick(candidates, now=now)

        # Unknown keys get the mean of known keys unless the caller pinned a value.
        unknown_default = self._unknown_weight
        if unknown_default is None:
            unknown_default = sum(known) / len(known)

        raw_weights = [
            (r if r is not None else unknown_default) for r in remainings
        ]
        # Apply softmax-style temperature to flatten/sharpen the distribution.
        # temperature→∞  => uniform; temperature→0 => argmax-like.
        adj = [max(w, self._min_weight) ** (1.0 / self._temperature) for w in raw_weights]
        total = sum(adj)
        if total <= 0:
            return self._fallback.pick(candidates, now=now)

        r = self._rng.random() * total
        cum = 0.0
        for cand, w in zip(candidates, adj):
            cum += w
            if r <= cum:
                return cand
        return candidates[-1]  # float rounding safety net


class WeightedRandomSelector:
    """Weighted random with configurable uniform blending.

    `tolerance` controls how much entropy to add:
        tolerance = 0.0  => deterministic round-robin
        tolerance = 1.0  => uniform random
        in between       => blend of round-robin cursor and uniform random

    This is borrowed from Mirrowel/LLM-API-Key-Proxy's ROTATION_TOLERANCE
    knob. The point is to avoid perfectly predictable sequences that a
    backend's abuse-detection heuristics could pattern-match on.
    """

    def __init__(self, tolerance: float = 0.3) -> None:
        self._tolerance = max(0.0, min(1.0, tolerance))
        self._rr = RoundRobinSelector()
        self._rng = random.Random()

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        if not candidates:
            raise ValueError("no candidates")
        if self._rng.random() < self._tolerance:
            return self._rng.choice(candidates)
        return self._rr.pick(candidates, now=now)


class SelectorChain:
    """Try a primary selector; if it raises or returns None, use fallback.

    Useful for: "use WeightedByCredits when quota_monitor is warm,
    otherwise round-robin".
    """

    def __init__(self, primary: Selector, fallback: Selector) -> None:
        self._primary = primary
        self._fallback = fallback

    def pick(self, candidates: List[_AccountLike], *, now: float) -> _AccountLike:
        try:
            result = self._primary.pick(candidates, now=now)
            if result is not None:
                return result
        except Exception:
            pass
        return self._fallback.pick(candidates, now=now)


# ---------------------------------------------------------------- semaphore


class PerAccountSemaphore:
    """Async concurrency limiter keyed by account name/key.

    Kiro's upstream doesn't publish hard per-key QPS numbers, but running
    more than ~5 concurrent requests on one key reliably triggers
    ThrottlingException. This gate caps in-flight requests per account
    without serializing the whole pool.

    Usage:
        sem = PerAccountSemaphore(limit_per_account=4)
        async with sem.slot(account.key):
            await do_upstream_call(...)

    `stats()` returns {key: (in_flight, limit)} for dashboarding.
    """

    def __init__(self, limit_per_account: int = 4) -> None:
        if limit_per_account < 1:
            raise ValueError("limit_per_account must be >= 1")
        self._limit = limit_per_account
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._in_flight: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _get(self, key: str) -> asyncio.Semaphore:
        # Lock-free fast path; asyncio.Semaphore access is itself task-safe.
        s = self._sems.get(key)
        if s is None:
            s = asyncio.Semaphore(self._limit)
            self._sems[key] = s
            self._in_flight[key] = 0
        return s

    def slot(self, key: str) -> "_SemSlot":
        return _SemSlot(self, key)

    def in_flight(self, key: str) -> int:
        return self._in_flight.get(key, 0)

    def stats(self) -> Dict[str, Tuple[int, int]]:
        return {k: (self._in_flight.get(k, 0), self._limit) for k in self._sems}


class _SemSlot:
    """Async context manager for a single slot on a specific key."""

    def __init__(self, owner: PerAccountSemaphore, key: str) -> None:
        self._owner = owner
        self._key = key
        self._sem: Optional[asyncio.Semaphore] = None

    async def __aenter__(self) -> "_SemSlot":
        self._sem = self._owner._get(self._key)
        await self._sem.acquire()
        self._owner._in_flight[self._key] = self._owner._in_flight.get(self._key, 0) + 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._owner._in_flight[self._key] = max(
            0, self._owner._in_flight.get(self._key, 0) - 1
        )
        if self._sem is not None:
            self._sem.release()


# ---------------------------------------------------------------- factory


def make_selector(
    name: Optional[str] = None,
    *,
    get_remaining: Optional[RemainingCreditsFn] = None,
    tolerance: Optional[float] = None,
) -> Selector:
    """Build a selector by name, reading env vars as defaults.

    Env vars:
        KIROGATE_ROTATION_STRATEGY = round_robin | least_used
                                   | weighted_credits | weighted_random
        KIROGATE_ROTATION_TOLERANCE = 0.0..1.0 (for weighted_random)

    weighted_credits requires `get_remaining`; if not provided, this
    function falls back to round_robin silently rather than crash at
    import time.
    """
    if name is None:
        name = os.environ.get("KIROGATE_ROTATION_STRATEGY", "round_robin")
    name = name.strip().lower()
    if tolerance is None:
        try:
            tolerance = float(os.environ.get("KIROGATE_ROTATION_TOLERANCE", "0.2"))
        except ValueError:
            tolerance = 0.2

    if name in ("rr", "round_robin"):
        return RoundRobinSelector()
    if name in ("lru", "least_used"):
        return LeastUsedSelector()
    if name in ("weighted_credits", "credits", "quota"):
        if get_remaining is None:
            return RoundRobinSelector()
        return SelectorChain(
            primary=WeightedByCreditsSelector(get_remaining),
            fallback=RoundRobinSelector(),
        )
    if name in ("weighted_random", "jitter", "random"):
        return WeightedRandomSelector(tolerance=tolerance or 0.3)
    # Unknown -> safe default
    return RoundRobinSelector()


# ---------------------------------------------------------------- self-test


def _selftest() -> None:  # pragma: no cover - dev smoke test
    @dataclass
    class _A:
        key: str
        name: str
        cooldown_until: float = 0.0
        total_requests: int = 0
        total_failures: int = 0

    accts = [_A(key=f"k{i}", name=f"n{i}") for i in range(3)]
    now = time.time()

    rr = RoundRobinSelector()
    picks = [rr.pick(accts, now=now).name for _ in range(6)]
    assert picks == ["n0", "n1", "n2", "n0", "n1", "n2"], picks

    accts[0].total_requests = 10
    accts[1].total_requests = 2
    accts[2].total_requests = 5
    lu = LeastUsedSelector()
    assert lu.pick(accts, now=now).name == "n1"

    quotas = {"k0": 500.0, "k1": 50.0, "k2": 5.0}
    wc = WeightedByCreditsSelector(quotas.get, softmax_temperature=1.0)
    # Over many picks, k0 should dominate
    tally = {"n0": 0, "n1": 0, "n2": 0}
    for _ in range(5000):
        tally[wc.pick(accts, now=now).name] += 1
    assert tally["n0"] > tally["n1"] > tally["n2"], tally

    wr = WeightedRandomSelector(tolerance=1.0)  # pure random
    seen = {wr.pick(accts, now=now).name for _ in range(200)}
    assert seen == {"n0", "n1", "n2"}, seen

    # Selector chain falls back when primary fails
    class _Boom:
        def pick(self, _c, *, now):
            raise RuntimeError("x")

    chained = SelectorChain(_Boom(), RoundRobinSelector())
    assert chained.pick(accts, now=now).name in {"n0", "n1", "n2"}

    # make_selector env-var behavior
    os.environ["KIROGATE_ROTATION_STRATEGY"] = "least_used"
    assert isinstance(make_selector(), LeastUsedSelector)
    os.environ.pop("KIROGATE_ROTATION_STRATEGY", None)

    print("pool_strategies selftest OK")


if __name__ == "__main__":
    _selftest()
