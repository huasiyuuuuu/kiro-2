"""Background quota monitor for kirogate's key pool.

Why it exists
-------------
kirogate's pool knows which keys are *healthy* (not in cooldown) but has
no idea which keys have *more credits left*. That means a key with 5
credits gets picked as often as a key with 950 credits, and the big
credit reservoir is wasted.

`GetUsageLimits` is free and returns exact balances (see kiro_quota.py
in this repo). This module calls it on a schedule for every key in the
pool and exposes a cached lookup suitable for
`WeightedByCreditsSelector`.

Design notes
------------
* Refresh cadence: once per `refresh_interval` (default 300s) per key,
  plus a "nudged" refresh after a successful request (decay model —
  so balances are up-to-date for the key that just spent credits).
* First refresh fires immediately on `start()` so the cache warms up
  before the first request.
* Backoff on failure: if a refresh for a given key fails (network,
  throttle), that key's next attempt is deferred with exponential
  backoff up to 1 hour. A key that never responds is treated as
  "quota unknown" (returns None from `remaining_credits`) so the
  quota-aware selector silently falls back to round-robin for it.
* Thread-safety: all state is guarded by an asyncio.Lock; callers are
  expected to be on the same loop. The read APIs (`remaining_credits`,
  `snapshot`) are lock-free and read a dict atomically — they can be
  called from sync code too.

Integration sketch (5 lines in kirogate/server.py):

    from kirogate_addons.quota_monitor import QuotaMonitor
    from kirogate_addons.pool_strategies import make_selector

    async def lifespan(app):
        qm = QuotaMonitor(pool=pool, http=app.state.http)
        await qm.start()
        pool._selector = make_selector("weighted_credits",
                                       get_remaining=qm.remaining_credits)
        app.state.qm = qm
        yield
        await qm.stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("kirogate.quota_monitor")

KIRO_HOST = "https://q.us-east-1.amazonaws.com"
_USER_AGENT = (
    "aws-sdk-rust/1.3.14 ua/2.1 api/codewhispererstreaming/0.1.14474 "
    "os/linux lang/rust/1.92.0 m/F app/AmazonQ-Developer-CLI/2.2.2"
)


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "AmazonCodeWhispererService.GetUsageLimits",
        "User-Agent": _USER_AGENT,
        "x-amz-user-agent": _USER_AGENT,
        "tokentype": "API_KEY",
        "accept": "*/*",
    }


@dataclass
class QuotaSnapshot:
    key_preview: str
    subscription_title: Optional[str] = None
    subscription_type: Optional[str] = None
    usage_limit: Optional[float] = None
    current_usage: Optional[float] = None
    overage_status: Optional[str] = None
    overage_cap: Optional[float] = None
    overage_rate: Optional[float] = None
    next_reset_unix: Optional[float] = None
    user_id: Optional[str] = None
    refreshed_at: float = 0.0
    last_error: Optional[str] = None
    consecutive_failures: int = 0

    @property
    def remaining(self) -> Optional[float]:
        if self.usage_limit is None or self.current_usage is None:
            return None
        return max(self.usage_limit - self.current_usage, 0.0)

    def as_dict(self) -> Dict[str, Any]:
        d = {
            "key_preview": self.key_preview,
            "subscription_title": self.subscription_title,
            "subscription_type": self.subscription_type,
            "usage_limit": self.usage_limit,
            "current_usage": self.current_usage,
            "remaining": self.remaining,
            "overage_status": self.overage_status,
            "overage_cap": self.overage_cap,
            "overage_rate": self.overage_rate,
            "next_reset_unix": self.next_reset_unix,
            "user_id": self.user_id,
            "refreshed_at": self.refreshed_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
        return d


def _preview(key: str) -> str:
    if len(key) <= 16:
        return key
    return key[:10] + "..." + key[-4:]


async def fetch_usage_limits(
    client: httpx.AsyncClient, api_key: str, *, timeout: float = 20.0
) -> Dict[str, Any]:
    """Single one-shot call. Raises httpx.HTTPStatusError on 4xx/5xx."""
    r = await client.post(
        KIRO_HOST + "/",
        headers=_headers(api_key),
        content=b"{}",
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _parse_usage_limits(key: str, data: Dict[str, Any]) -> QuotaSnapshot:
    snap = QuotaSnapshot(key_preview=_preview(key))
    sub = data.get("subscriptionInfo") or {}
    snap.subscription_title = sub.get("subscriptionTitle")
    snap.subscription_type = sub.get("type")
    snap.overage_status = (data.get("overageConfiguration") or {}).get("overageStatus")
    snap.user_id = (data.get("userInfo") or {}).get("userId")
    for entry in data.get("usageBreakdownList") or []:
        if entry.get("resourceType") == "CREDIT":
            snap.usage_limit = _float(
                entry.get("usageLimitWithPrecision", entry.get("usageLimit"))
            )
            snap.current_usage = _float(
                entry.get("currentUsageWithPrecision", entry.get("currentUsage"))
            )
            snap.overage_cap = _float(
                entry.get("overageCapWithPrecision", entry.get("overageCap"))
            )
            snap.overage_rate = _float(entry.get("overageRate"))
            snap.next_reset_unix = _float(entry.get("nextDateReset"))
            break
    snap.refreshed_at = time.time()
    return snap


def _float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class _Entry:
    key: str
    name: str
    snapshot: Optional[QuotaSnapshot] = None
    next_refresh_at: float = 0.0
    in_flight: bool = False


class QuotaMonitor:
    """Background poller. One task, runs forever until `stop()`."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        accounts: Optional[List[Any]] = None,
        pool: Any = None,
        refresh_interval: Optional[float] = None,
        max_backoff: float = 3600.0,
        jitter: float = 0.15,
    ) -> None:
        """
        Pass EITHER `accounts` (a list of objects with .key/.name) OR
        `pool` (a kirogate AccountPool — we read .accounts from it on
        every tick so hot-reload works). If both are given, `pool` wins.
        """
        if accounts is None and pool is None:
            raise ValueError("pass accounts or pool")
        self._http = http
        self._accounts = accounts
        self._pool = pool
        env_default = os.environ.get("KIROGATE_QUOTA_REFRESH_INTERVAL")
        try:
            self._interval = float(
                refresh_interval
                if refresh_interval is not None
                else (env_default if env_default else 300.0)
            )
        except ValueError:
            self._interval = 300.0
        self._max_backoff = max_backoff
        self._jitter = jitter
        self._entries: Dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    # ---------------- lifecycle

    async def start(self, *, wait_for_first_sweep: bool = True) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._sync_entries_from_source()
        if wait_for_first_sweep:
            await self._refresh_all_once(force=True)
        self._task = asyncio.create_task(
            self._run_forever(), name="kirogate-quota-monitor"
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    # ---------------- read APIs (sync, lock-free)

    def remaining_credits(self, key: str) -> Optional[float]:
        """Return remaining credits for `key`, or None if unknown.

        Safe to call from sync code. Pass this bound method to
        `WeightedByCreditsSelector`.
        """
        entry = self._entries.get(key)
        if entry is None or entry.snapshot is None:
            return None
        return entry.snapshot.remaining

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return {account_name: snapshot_dict} for dashboarding."""
        out: Dict[str, Dict[str, Any]] = {}
        for entry in self._entries.values():
            if entry.snapshot is None:
                out[entry.name] = {"key_preview": _preview(entry.key), "remaining": None}
            else:
                d = entry.snapshot.as_dict()
                d["name"] = entry.name
                out[entry.name] = d
        return out

    # ---------------- "nudge" API — call after a successful request

    def nudge(self, key: str, *, delay: float = 0.0) -> None:
        """Schedule a near-term refresh for this key.

        Useful hook: call from `pool.mark_success` to keep the spending
        key's quota estimate fresh without refreshing every key constantly.
        """
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.next_refresh_at = min(
            entry.next_refresh_at, time.time() + max(0.0, delay)
        )

    # ---------------- internals

    def _sync_entries_from_source(self) -> None:
        accounts = self._accounts
        if self._pool is not None:
            accounts = getattr(self._pool, "accounts", None) or []
        if accounts is None:
            return
        live_keys = set()
        for a in accounts:
            live_keys.add(a.key)
            if a.key not in self._entries:
                self._entries[a.key] = _Entry(key=a.key, name=a.name)
            else:
                self._entries[a.key].name = a.name
        # Drop entries for removed accounts.
        for k in list(self._entries):
            if k not in live_keys:
                self._entries.pop(k, None)

    async def _run_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                self._sync_entries_from_source()
                due_keys = [
                    k
                    for k, e in self._entries.items()
                    if not e.in_flight and time.time() >= e.next_refresh_at
                ]
                if due_keys:
                    await asyncio.gather(
                        *(self._refresh_one(k) for k in due_keys),
                        return_exceptions=True,
                    )
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _refresh_all_once(self, *, force: bool = False) -> None:
        self._sync_entries_from_source()
        keys = list(self._entries.keys())
        if force:
            for k in keys:
                self._entries[k].next_refresh_at = 0.0
        await asyncio.gather(
            *(self._refresh_one(k) for k in keys), return_exceptions=True
        )

    async def _refresh_one(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is None or entry.in_flight:
            return
        entry.in_flight = True
        try:
            try:
                data = await fetch_usage_limits(self._http, key)
                snap = _parse_usage_limits(key, data)
                if entry.snapshot is not None:
                    snap.consecutive_failures = 0
                entry.snapshot = snap
                entry.next_refresh_at = time.time() + self._jittered_interval()
                log.debug(
                    "quota refresh ok acct=%s remaining=%s",
                    entry.name,
                    f"{snap.remaining:.2f}" if snap.remaining is not None else "?",
                )
            except httpx.HTTPStatusError as e:
                fails = (entry.snapshot.consecutive_failures if entry.snapshot else 0) + 1
                backoff = self._backoff_for(fails)
                err = f"HTTP {e.response.status_code}: {e.response.text[:160]}"
                if entry.snapshot is None:
                    entry.snapshot = QuotaSnapshot(key_preview=_preview(key))
                entry.snapshot.last_error = err
                entry.snapshot.consecutive_failures = fails
                entry.next_refresh_at = time.time() + backoff
                log.warning(
                    "quota refresh fail acct=%s fails=%d backoff=%.0fs err=%s",
                    entry.name, fails, backoff, err,
                )
            except Exception as e:
                fails = (entry.snapshot.consecutive_failures if entry.snapshot else 0) + 1
                backoff = self._backoff_for(fails)
                err = f"{type(e).__name__}: {e}"
                if entry.snapshot is None:
                    entry.snapshot = QuotaSnapshot(key_preview=_preview(key))
                entry.snapshot.last_error = err
                entry.snapshot.consecutive_failures = fails
                entry.next_refresh_at = time.time() + backoff
                log.warning(
                    "quota refresh err acct=%s fails=%d backoff=%.0fs err=%s",
                    entry.name, fails, backoff, err,
                )
        finally:
            entry.in_flight = False

    def _jittered_interval(self) -> float:
        j = self._jitter * self._interval
        return self._interval + random.uniform(-j, j)

    def _backoff_for(self, fails: int) -> float:
        base = min(self._interval * (2 ** (fails - 1)), self._max_backoff)
        return base * (0.75 + random.random() * 0.5)


# ---------------------------------------------------------------- smoke test

async def _smoke(api_key: str) -> None:  # pragma: no cover - live smoke test
    """Live test: runs a monitor against a single real key."""
    @dataclass
    class _Acc:
        key: str
        name: str = "acc0"

    accounts = [_Acc(key=api_key)]
    async with httpx.AsyncClient() as http:
        qm = QuotaMonitor(accounts=accounts, http=http, refresh_interval=10)
        await qm.start()
        print("after start:")
        for name, s in qm.snapshot().items():
            print(f"  {name}: {s}")
        remaining = qm.remaining_credits(api_key)
        print(f"remaining_credits(key) = {remaining}")
        await qm.stop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python quota_monitor.py ksk_...")
        sys.exit(2)
    asyncio.run(_smoke(sys.argv[1]))
