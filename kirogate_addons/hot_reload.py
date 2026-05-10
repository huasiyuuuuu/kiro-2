"""Hot-reload for kirogate's account pool.

Kirogate loads `KIRO_ACCOUNTS_FILE` (or `KIRO_API_KEYS` env) exactly
once at startup. To add, remove, or rotate keys you have to restart the
process — which drops every in-flight streaming request. Ugly.

This module watches the accounts file's mtime and applies changes in
place. New keys are appended; removed keys are dropped; existing keys'
runtime state (cooldowns, stats) is preserved so you don't lose history
mid-flight.

It's a filesystem poller, not inotify — portable to Docker volumes,
macOS, Windows, and NFS without extra deps.

Integration sketch (3 lines in kirogate/server.py::lifespan):

    from kirogate_addons.hot_reload import AccountsFileWatcher
    watcher = AccountsFileWatcher(pool, path=settings.accounts_file)
    await watcher.start()
    ...
    await watcher.stop()

If you also use QuotaMonitor, pass `pool=pool` to it (not `accounts=`):
the monitor picks up pool mutations on its next tick automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("kirogate.hot_reload")


_KEY_PREFIXES = ("ksk_",)


def _looks_like_key(s: str) -> bool:
    return isinstance(s, str) and any(s.startswith(p) for p in _KEY_PREFIXES)


def parse_accounts_file(path: str) -> List[Tuple[str, str]]:
    """Return [(key, name), ...] parsed from a kirogate-compatible accounts file.

    Accepted formats:
        ["ksk_aaa", "ksk_bbb"]
        [{"key": "ksk_aaa", "name": "primary"}, ...]
        mixed of the two
        plain text, one key per line (# comments allowed)

    Raises ValueError on malformed input (missing JSON close, unknown
    top-level type, or a plain-text line that doesn't look like a key).
    Hot-reload's policy is to swallow these and keep the previous pool,
    so a bad edit never takes the gateway down.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    # Treat anything that smells like JSON strictly — no silent fallback.
    first = text[0]
    if first in "[{":
        parsed = json.loads(text)  # raises json.JSONDecodeError on bad JSON
        if not isinstance(parsed, list):
            raise ValueError(f"expected a JSON array at top level, got {type(parsed).__name__}")
        out = []
        for i, item in enumerate(parsed):
            if isinstance(item, str):
                if not _looks_like_key(item):
                    raise ValueError(f"entry {i} does not look like a ksk_ key: {item[:20]!r}")
                out.append((item, f"acc{i}"))
            elif isinstance(item, dict):
                k = item.get("key")
                if not _looks_like_key(k):
                    raise ValueError(f"entry {i} missing or malformed 'key'")
                out.append((k, item.get("name") or f"acc{i}"))
            else:
                raise ValueError(f"entry {i} has unsupported type {type(item).__name__}")
        return out
    # plain text
    out = []
    idx = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not _looks_like_key(line):
            raise ValueError(f"line {idx + 1} does not look like a ksk_ key")
        out.append((line, f"acc{idx}"))
        idx += 1
    return out


def apply_accounts_to_pool(pool: Any, desired: List[Tuple[str, str]]) -> Dict[str, int]:
    """Mutate `pool.accounts` so it matches `desired`, preserving state.

    Returns {"added": X, "removed": Y, "renamed": Z, "total": T}.

    Contract assumptions about `pool`:
      - pool._lock is a threading.Lock (or has context-manager semantics)
      - pool.accounts is a list of objects with .key, .name, runtime fields
      - the Account class can be constructed as Account(key=..., name=...)

    We locate the Account class via type(pool.accounts[0]) if the pool is
    non-empty; otherwise we fall back to a lightweight SimpleNamespace-like
    type. That covers the real kirogate.Account plus the test doubles this
    repo uses.
    """
    lock = getattr(pool, "_lock", None)
    # Resolve the Account constructor.
    ctor: Callable[..., Any]
    if pool.accounts:
        ctor = type(pool.accounts[0])
    else:
        try:
            # Best-effort import of kirogate's real class.
            from kirogate.pool import Account as _RealAccount  # type: ignore
            ctor = _RealAccount
        except Exception:
            from dataclasses import dataclass

            @dataclass
            class _FallbackAccount:
                key: str
                name: str = ""
                cooldown_until: float = 0.0
                consecutive_failures: int = 0
                total_requests: int = 0
                total_failures: int = 0
                last_error: str = ""

            ctor = _FallbackAccount

    added = removed = renamed = 0

    def _swap():
        nonlocal added, removed, renamed
        by_key = {a.key: a for a in pool.accounts}
        desired_keys = [k for k, _ in desired]
        new_list: List[Any] = []
        for k, n in desired:
            existing = by_key.get(k)
            if existing is not None:
                if n and existing.name != n:
                    existing.name = n
                    renamed += 1
                new_list.append(existing)
            else:
                new_list.append(ctor(key=k, name=n))
                added += 1
        removed = len(pool.accounts) - (len(new_list) - added)
        pool.accounts = new_list
        # Reset round-robin cursor if it now points past the end; safe
        # because the next pick() will wrap anyway, but keeping it clean
        # avoids off-by-one confusion in logs.
        if hasattr(pool, "_rr_idx"):
            try:
                if pool._rr_idx >= len(new_list):
                    pool._rr_idx = -1
            except Exception:
                pass
        # Prune stale sticky bindings.
        if hasattr(pool, "_sticky") and isinstance(pool._sticky, dict):
            stale = [
                k for k, v in pool._sticky.items()
                if isinstance(v, tuple) and v and (
                    (v[0] if isinstance(v[0], int) else -1) >= len(new_list)
                )
            ]
            for k in stale:
                pool._sticky.pop(k, None)
        return {
            "added": added,
            "removed": max(removed, 0),
            "renamed": renamed,
            "total": len(new_list),
        }

    if lock is not None and hasattr(lock, "__enter__"):
        with lock:
            return _swap()
    return _swap()


class AccountsFileWatcher:
    """Polls a file's mtime and re-applies it to `pool` when it changes.

    On-change policy: if parsing fails OR the new list is empty, we log
    loudly and keep the previous state. We never leave the pool with zero
    accounts by accident.

    Callbacks:
        on_reload(summary) — called after each successful apply with the
            dict returned by `apply_accounts_to_pool`. Useful for
            dashboards / metrics.
    """

    def __init__(
        self,
        pool: Any,
        path: str,
        *,
        poll_interval: float = 2.0,
        on_reload: Optional[Callable[[Dict[str, int]], None]] = None,
    ) -> None:
        self._pool = pool
        self._path = path
        self._poll = max(0.5, poll_interval)
        self._on_reload = on_reload
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._last_mtime: Optional[float] = None

    async def start(self, *, apply_current: bool = False) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        if apply_current and self._path and os.path.exists(self._path):
            try:
                desired = parse_accounts_file(self._path)
                if desired:
                    apply_accounts_to_pool(self._pool, desired)
            except Exception as e:
                log.warning("initial accounts load failed: %s", e)
        if self._path and os.path.exists(self._path):
            self._last_mtime = os.path.getmtime(self._path)
        self._task = asyncio.create_task(
            self._run_forever(), name="kirogate-hot-reload"
        )
        log.info("accounts file watcher started: %s (poll=%.1fs)",
                 self._path, self._poll)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._poll
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            m = os.path.getmtime(self._path)
        except OSError:
            return
        if self._last_mtime is not None and m == self._last_mtime:
            return
        self._last_mtime = m
        try:
            desired = parse_accounts_file(self._path)
        except Exception as e:
            log.error("refusing to reload; parse failed: %s", e)
            return
        if not desired:
            log.warning("refusing to reload; new file would empty the pool")
            return
        summary = apply_accounts_to_pool(self._pool, desired)
        log.info(
            "accounts reloaded: added=%d removed=%d renamed=%d total=%d",
            summary["added"], summary["removed"], summary["renamed"], summary["total"],
        )
        if self._on_reload is not None:
            try:
                result = self._on_reload(summary)
                # Accept either a sync callback or `async def on_reload`.
                # Previously async callbacks were silently dropped (we
                # got an un-awaited coroutine and logged a bogus warning).
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.warning("on_reload callback raised: %s", e)


# ---------------------------------------------------------------- selftest

async def _selftest(tmpdir: str) -> None:  # pragma: no cover - dev test
    import tempfile
    import threading
    from dataclasses import dataclass

    @dataclass
    class _Account:
        key: str
        name: str = ""
        cooldown_until: float = 0.0
        consecutive_failures: int = 0
        total_requests: int = 0
        total_failures: int = 0
        last_error: str = ""

    class _Pool:
        def __init__(self, accounts):
            self.accounts = accounts
            self._lock = threading.Lock()
            self._rr_idx = 0
            self._sticky = {}

    pool = _Pool([_Account(key="ksk_a", name="a", total_requests=10),
                  _Account(key="ksk_b", name="b", total_requests=5)])

    path = os.path.join(tmpdir, "accounts.json")
    with open(path, "w") as f:
        json.dump(["ksk_a", "ksk_b"], f)

    reloads = []
    watcher = AccountsFileWatcher(
        pool, path=path, poll_interval=0.2,
        on_reload=lambda s: reloads.append(s),
    )
    await watcher.start()
    await asyncio.sleep(0.1)

    # 1) Replace ksk_a with ksk_c; keep ksk_b. Expect: ksk_b's state preserved.
    time.sleep(0.05)
    with open(path, "w") as f:
        json.dump([{"key": "ksk_b", "name": "bob"},
                   {"key": "ksk_c", "name": "carol"}], f)
    await asyncio.sleep(0.6)

    assert [a.key for a in pool.accounts] == ["ksk_b", "ksk_c"], [
        a.key for a in pool.accounts
    ]
    b = next(a for a in pool.accounts if a.key == "ksk_b")
    assert b.total_requests == 5, "state lost on reload"
    assert b.name == "bob", "rename not applied"
    assert reloads and reloads[-1]["added"] == 1 and reloads[-1]["removed"] == 1, reloads

    # 2) Corrupt file — pool must NOT be emptied.
    with open(path, "w") as f:
        f.write("{{ not json")
    await asyncio.sleep(0.6)
    assert len(pool.accounts) == 2, "parse error should not empty pool"

    # 3) Empty list — also rejected.
    with open(path, "w") as f:
        json.dump([], f)
    await asyncio.sleep(0.6)
    assert len(pool.accounts) == 2, "empty file should not empty pool"

    await watcher.stop()
    print("hot_reload selftest OK")


if __name__ == "__main__":
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(_selftest(d))
