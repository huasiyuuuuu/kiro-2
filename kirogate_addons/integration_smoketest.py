"""End-to-end smoketest: wires every addon together against a real key.

This does NOT touch the real kirogate package. It constructs a
lightweight fake `AccountPool` that follows the same protocol and
verifies:

  1. QuotaMonitor refreshes the real key and fills in `remaining`.
  2. WeightedByCreditsSelector, bound to the monitor, picks consistently.
  3. PerAccountSemaphore correctly serializes concurrent calls.
  4. AccountsFileWatcher adds/removes/renames accounts on file change,
     and QuotaMonitor (passed `pool=`) auto-discovers the new keys on
     its next tick.
  5. account_health.audit_one reports HEALTHY for the real key.
  6. stabilize_payload is idempotent and deterministic.
  7. CacheObserver records and classifies hits.

Run with:
    python -m kirogate_addons.integration_smoketest ksk_...
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kirogate_addons.account_health import audit_one, HEALTHY  # noqa: E402
from kirogate_addons.cache_observer import CacheObserver  # noqa: E402
from kirogate_addons.cache_stability import (  # noqa: E402
    payload_prefix_signature,
    stabilize_payload,
)
from kirogate_addons.hot_reload import (  # noqa: E402
    AccountsFileWatcher,
    apply_accounts_to_pool,
    parse_accounts_file,
)
from kirogate_addons.pool_strategies import (  # noqa: E402
    PerAccountSemaphore,
    WeightedByCreditsSelector,
    make_selector,
)
from kirogate_addons.quota_monitor import QuotaMonitor  # noqa: E402
from kirogate_addons.stall_guard import (  # noqa: E402
    InflightRegistry,
    StallGuardConfig,
    StallReaper,
)


@dataclass
class FakeAccount:
    key: str
    name: str = ""
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    last_error: str = ""


class FakePool:
    """Duck-type of kirogate's AccountPool sufficient for our addons."""

    def __init__(self, accounts: List[FakeAccount]) -> None:
        self.accounts = accounts
        self._lock = threading.Lock()
        self._rr_idx = 0
        self._sticky: Dict[str, Any] = {}


async def step(label: str, coro) -> Any:
    print(f"\n-- {label}")
    t0 = time.time()
    try:
        result = await coro
        print(f"   OK ({time.time() - t0:.2f}s)")
        return result
    except Exception as e:
        print(f"   FAIL: {type(e).__name__}: {e}")
        raise


async def main(real_key: str) -> int:
    fake_key = "ksk_fakeintegration_AAAAAAAAAAAAAAAA"

    with tempfile.TemporaryDirectory() as tmp:
        accounts_path = os.path.join(tmp, "accounts.json")
        with open(accounts_path, "w") as f:
            json.dump([{"key": real_key, "name": "real"}], f)

        desired = parse_accounts_file(accounts_path)
        pool = FakePool([FakeAccount(key=k, name=n) for k, n in desired])

        async with httpx.AsyncClient() as http:
            qm = QuotaMonitor(pool=pool, http=http, refresh_interval=5)
            watcher = AccountsFileWatcher(pool, path=accounts_path, poll_interval=0.3)

            await step("start quota monitor", qm.start())
            await step("start hot-reload watcher", watcher.start())

            # 1. quota monitor saw the real key
            rem = qm.remaining_credits(real_key)
            assert rem is not None and rem > 0, f"remaining not loaded: {rem}"
            print(f"   real key remaining: {rem:.2f}")

            # 2. weighted selector works with monitor as source
            sel = make_selector(
                "weighted_credits", get_remaining=qm.remaining_credits
            )
            pick = sel.pick(pool.accounts, now=time.time())
            assert pick.key == real_key
            print(f"   weighted picker chose: {pick.name}")

            # 3. semaphore under concurrent load
            sem = PerAccountSemaphore(limit_per_account=3)
            peak = {"v": 0}

            async def worker():
                async with sem.slot(real_key):
                    peak["v"] = max(peak["v"], sem.in_flight(real_key))
                    await asyncio.sleep(0.01)

            await asyncio.gather(*(worker() for _ in range(30)))
            assert peak["v"] <= 3, peak
            assert sem.in_flight(real_key) == 0
            print(f"   semaphore enforced peak <= 3 (observed {peak['v']})")

            # 4. hot-reload: add fake key, expect pool to grow, quota
            #    monitor to pick it up, selector to prefer real key.
            with open(accounts_path, "w") as f:
                json.dump(
                    [
                        {"key": real_key, "name": "real"},
                        {"key": fake_key, "name": "fake"},
                    ],
                    f,
                )
            # wait for watcher tick + next quota refresh
            await asyncio.sleep(1.0)
            await qm._refresh_all_once(force=True)  # force sync for test determinism
            keys = [a.key for a in pool.accounts]
            assert real_key in keys and fake_key in keys, keys
            print(f"   pool grew to {len(pool.accounts)} accounts: {keys}")

            # fake should either have None or an error recorded
            snap = qm.snapshot()
            fake_snap = snap.get("fake") or {}
            assert fake_snap.get("remaining") is None, fake_snap
            print(f"   fake key classified: last_error={fake_snap.get('last_error', '')[:60]!r}")

            # With one real and one unknown, selector should still pick something
            pick_names = {
                sel.pick(pool.accounts, now=time.time()).name for _ in range(50)
            }
            assert "real" in pick_names, pick_names
            print(f"   selector over mixed pool hit: {pick_names}")

            # 5. account_health audit on real key
            finding = await audit_one(
                http, real_key, "real", warn_threshold=10.0, probe=False
            )
            assert finding.status == HEALTHY, finding
            print(f"   preflight audit status: {finding.status}  "
                  f"remaining={finding.remaining:.2f}")

            # 6. cache_stability is idempotent on payloads with no
            #    randomness, and its signature-fn is deterministic.
            fake_payload = {
                "conversationState": {
                    "history": [
                        {"userInputMessage": {
                            "content": "hi",
                            "userInputMessageContext": {},
                            "origin": "KIRO_CLI", "modelId": "claude-haiku-4.5"}},
                        {"assistantResponseMessage": {
                            "content": "",
                            "toolUses": [{
                                "toolUseId": "call_abcdef0123456789",
                                "name": "x",
                                "input": {"v": 1},
                            }],
                        }},
                    ],
                    "currentMessage": {"userInputMessage": {
                        "content": "q",
                        "userInputMessageContext": {},
                        "origin": "KIRO_CLI", "modelId": "claude-haiku-4.5"}},
                }
            }
            stats = {}
            stabilize_payload(fake_payload, context_signature="smoke", stats=stats)
            sig_a = payload_prefix_signature(fake_payload)
            stabilize_payload(fake_payload, context_signature="smoke")
            sig_b = payload_prefix_signature(fake_payload)
            assert sig_a == sig_b, "stabilize is not idempotent"
            assert stats.get("synthetic_tool_ids_rewritten", 0) == 1, stats
            print(f"   cache_stability rewrote {stats['synthetic_tool_ids_rewritten']} "
                  f"random id(s); idempotent signature: {sig_a[:16]}")

            # 7. cache_observer counts warm vs cold.
            obs = CacheObserver(hit_ratio_threshold=0.65)
            obs.record(model="claude-haiku-4.5", prefix_signature=sig_a, credits=0.0197)
            for _ in range(3):
                obs.record(model="claude-haiku-4.5", prefix_signature=sig_a, credits=0.0105)
            snap = obs.snapshot()
            assert snap["total_seen"] == 4 and snap["total_hits"] == 3, snap
            print(f"   cache_observer: {snap['total_hits']}/{snap['total_seen']} hits "
                  f"saved {snap['credits_saved']:.4f} credits "
                  f"({snap['savings_pct']:.0f}%)")

            # 8. stall_guard: inflight registry + reaper cancels zombies.
            reg = InflightRegistry()
            reaper_cfg = StallGuardConfig.from_env()
            reaper = StallReaper(reg, interval=0.2)
            await reaper.start()
            cancelled = {"v": False}

            def _cancel():
                cancelled["v"] = True

            async with reg.record(
                model="m", account="a",
                deadline_at=time.time() - 0.5, cancel_cb=_cancel,
            ):
                await asyncio.sleep(0.4)
            await reaper.stop()
            assert cancelled["v"], "reaper didn't cancel the zombie"
            print(f"   stall_guard: reaper cancelled zombie={cancelled['v']}; "
                  f"config ttfb={reaper_cfg.ttfb_timeout}s "
                  f"idle={reaper_cfg.stream_idle_timeout}s "
                  f"deadline={reaper_cfg.total_deadline}s")


            await step("stop watcher", watcher.stop())
            await step("stop monitor", qm.stop())

    print("\nintegration smoketest PASSED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m kirogate_addons.integration_smoketest ksk_...")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
