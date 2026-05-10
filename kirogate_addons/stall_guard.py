"""Zombie-stream defense for kirogate.

The problem this solves
-----------------------
Kiro's upstream is fine in normal operation but occasionally produces
"zombie streams": the TCP connection stays open, httpx reports the
request as live, but no bytes ever arrive. The orchestrator (the
"包工头") sees a request it believes is in-flight forever. Total
request-level timeouts don't help because httpx's total timeout is
checked per I/O operation, not as a wallclock deadline; and its `read`
timeout only fires on inactivity at the socket layer, which can be
defeated by TCP keepalives or partial TLS records.

Real-world gap measurements against the live backend (see
experiments/probe_keepalive.py):

    short call        TTFB 0.4s  max inter-event gap 0.41s
    medium output     TTFB 0.5s  max inter-event gap 0.54s
    haiku reasoning   TTFB 0.7s  max inter-event gap 0.70s

Conclusion: in healthy operation, the longest plausible gap between
two decoded events is well under a second. Anything past ~5s is
evidence of a real hang, not a normal slow model.

Architecture (six layers of defense)
------------------------------------
1. httpx.Timeout tuned per phase (connect/write/pool short; read
   generous but bounded). Cost: tiny.
2. TTFB watchdog — waiting for the FIRST byte can take 30s+ on
   Opus; tolerate that but no more. Configurable (default 90s).
3. Inter-event watchdog — once bytes start flowing, any silence
   beyond `stream_idle_timeout` (default 30s) is a stall.
4. Total deadline — hard ceiling on a single request, independent
   of chunk activity (default 600s).
5. Inflight registry — every request records its state (account,
   model, phase, last-chunk-at, bytes, events). Lets the gateway
   expose /pool/inflight and a reaper find zombies.
6. Downstream disconnect watcher — if the client that called us
   walks away, cancel the upstream call immediately so we stop
   bleeding credits on a response nobody will ever read.

When a stall is detected we raise `StreamStalled` (a subclass of
`TimeoutError` AND of `httpx.HTTPError` so existing retry code
catches it). The underlying httpx stream and socket are eagerly
closed so connection-pool leaks cannot accumulate.

Integration sketch (kirogate side)
----------------------------------

    from kirogate_addons.stall_guard import (
        StallGuardConfig, InflightRegistry, guarded_stream, ClientDisconnect,
    )

    app.state.inflight = InflightRegistry()

    async def stream_to_client(request, kiro_resp, acct):
        cfg = StallGuardConfig.from_env()
        dropper = ClientDisconnect(request)  # FastAPI
        with app.state.inflight.record(
            model=..., account=acct.name, request_id=req_id
        ) as handle:
            async for chunk in guarded_stream(
                kiro_resp, cfg=cfg, handle=handle, on_stall=...,
                cancel_if=dropper.is_disconnected,
            ):
                yield chunk

    @app.get("/pool/inflight")
    async def inflight():
        return app.state.inflight.snapshot()

A CLI `kiro_inflight.py` (also in this repo) reads /pool/inflight and
prints a live dashboard, so the contractor that holds the master API
key can see at a glance which Kiro calls are really working and
which ones are zombies.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
)

import httpx

log = logging.getLogger("kirogate.stall_guard")


# ------------------------------------------------------------------ exceptions


class StreamStalled(httpx.HTTPError):
    """Raised when the upstream stream stops producing bytes for too long.

    Inherits from httpx.HTTPError so existing `except httpx.HTTPError`
    blocks in kirogate catch it automatically. We can't also inherit
    from the built-in TimeoutError because of C-level layout
    restrictions; instead we set __cause__ / consumers can match on
    `isinstance(e, StreamStalled)` or via the sentinel attribute
    `is_timeout=True` for duck-typed checks.
    """

    is_timeout = True

    def __init__(self, message: str, *, phase: str, waited: float) -> None:
        super().__init__(message)
        self.phase = phase
        self.waited = waited


class DeadlineExceeded(StreamStalled):
    """Raised when the total time budget for the request is exhausted."""


class ClientGone(httpx.HTTPError):
    """Raised when the downstream client disconnected mid-stream."""


# ------------------------------------------------------------------ config


@dataclass
class StallGuardConfig:
    """All knobs in one place. Defaults tuned for Kiro's observed latencies."""

    # httpx.Timeout components.
    connect: float = 10.0
    write: float = 30.0
    pool: float = 10.0
    # This is the SOCKET-level read timeout httpx uses; we set it
    # generously and enforce our own (tighter) application-level
    # watchdogs on top. Keeping this long avoids bubbling spurious
    # httpx.ReadTimeouts when the backend is just slow, not dead.
    socket_read: float = 120.0

    # Application-level watchdogs.
    ttfb_timeout: float = 90.0          # max time waiting for first byte
    stream_idle_timeout: float = 30.0   # max silence mid-stream
    total_deadline: float = 600.0       # hard ceiling per call

    # Downstream-client-disconnect polling interval.
    client_poll_interval: float = 1.0

    # Inflight registry reaper period.
    reap_interval: float = 5.0

    @classmethod
    def from_env(cls, prefix: str = "KIROGATE_") -> "StallGuardConfig":
        """Build from env vars. All optional.

        KIROGATE_CONNECT_TIMEOUT, KIROGATE_WRITE_TIMEOUT,
        KIROGATE_POOL_TIMEOUT, KIROGATE_SOCKET_READ_TIMEOUT,
        KIROGATE_TTFB_TIMEOUT, KIROGATE_STREAM_IDLE_TIMEOUT,
        KIROGATE_TOTAL_DEADLINE, KIROGATE_CLIENT_POLL_INTERVAL,
        KIROGATE_REAP_INTERVAL.
        """
        def _f(key: str, default: float) -> float:
            raw = os.environ.get(prefix + key)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError:
                log.warning("invalid %s=%r, using default %s", prefix + key, raw, default)
                return default

        return cls(
            connect=_f("CONNECT_TIMEOUT", 10.0),
            write=_f("WRITE_TIMEOUT", 30.0),
            pool=_f("POOL_TIMEOUT", 10.0),
            socket_read=_f("SOCKET_READ_TIMEOUT", 120.0),
            ttfb_timeout=_f("TTFB_TIMEOUT", 90.0),
            stream_idle_timeout=_f("STREAM_IDLE_TIMEOUT", 30.0),
            total_deadline=_f("TOTAL_DEADLINE", 600.0),
            client_poll_interval=_f("CLIENT_POLL_INTERVAL", 1.0),
            reap_interval=_f("REAP_INTERVAL", 5.0),
        )

    def httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.socket_read,
            write=self.write,
            pool=self.pool,
        )


# ------------------------------------------------------------------ inflight registry


@dataclass
class InflightHandle:
    """One row per in-flight request. Updated live; read by dashboards."""

    request_id: str
    model: str
    account: str
    started_at: float
    deadline_at: float
    phase: str = "pending"          # pending → connected → streaming → done | error | stalled
    last_activity_at: float = field(default_factory=time.time)
    bytes_received: int = 0
    events_received: int = 0
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    # Populated by InflightRegistry.record() — used to force-cancel on reap.
    _cancel_cb: Optional[Callable[[], None]] = None

    def touch(self, bytes_delta: int = 0, events_delta: int = 0) -> None:
        self.last_activity_at = time.time()
        if bytes_delta:
            self.bytes_received += bytes_delta
        if events_delta:
            self.events_received += events_delta

    def as_dict(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "request_id": self.request_id,
            "model": self.model,
            "account": self.account,
            "phase": self.phase,
            "age_sec": round(now - self.started_at, 2),
            "idle_sec": round(now - self.last_activity_at, 2),
            "remaining_sec": round(self.deadline_at - now, 2),
            "bytes": self.bytes_received,
            "events": self.events_received,
            "error": self.error,
            **{f"x_{k}": v for k, v in self.extra.items()},
        }


class InflightRegistry:
    """Thread-safe live registry of all in-flight upstream requests.

    Meant to be a long-lived singleton on the gateway. The `record()`
    context manager adds an entry on enter and removes it on exit,
    so the registry is always consistent.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, InflightHandle] = {}
        self._lock = asyncio.Lock()

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    @contextlib.asynccontextmanager
    async def record(
        self,
        *,
        model: str,
        account: str,
        deadline_at: float,
        request_id: Optional[str] = None,
        cancel_cb: Optional[Callable[[], None]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[InflightHandle]:
        rid = request_id or self._new_id()
        handle = InflightHandle(
            request_id=rid,
            model=model,
            account=account,
            started_at=time.time(),
            deadline_at=deadline_at,
            extra=dict(extra or {}),
        )
        handle._cancel_cb = cancel_cb
        async with self._lock:
            self._entries[rid] = handle
        try:
            yield handle
        finally:
            async with self._lock:
                self._entries.pop(rid, None)

    async def snapshot(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return [h.as_dict() for h in self._entries.values()]

    async def list_handles(self) -> List[InflightHandle]:
        async with self._lock:
            return list(self._entries.values())

    async def count(self) -> int:
        async with self._lock:
            return len(self._entries)

    async def prometheus(self) -> str:
        """Render inflight state as Prometheus text exposition format.

        The same metric set as kiro_inflight.py --prometheus, but
        available inside the gateway process itself so you can wire
        it into the existing /metrics endpoint without polling from
        outside.
        """
        rows = await self.snapshot()
        total = len(rows)
        by_phase: Dict[str, int] = {}
        max_age = 0.0
        max_idle = 0.0
        stalled = 0
        per_acct_bytes: Dict[str, int] = {}
        per_acct_events: Dict[str, int] = {}
        for r in rows:
            phase = r.get("phase", "?")
            by_phase[phase] = by_phase.get(phase, 0) + 1
            if phase in ("error", "stalled"):
                stalled += 1
            max_age = max(max_age, float(r.get("age_sec") or 0))
            max_idle = max(max_idle, float(r.get("idle_sec") or 0))
            acct = str(r.get("account") or "unknown").replace('"', '').replace("\\", "")
            per_acct_bytes[acct] = per_acct_bytes.get(acct, 0) + int(r.get("bytes") or 0)
            per_acct_events[acct] = per_acct_events.get(acct, 0) + int(r.get("events") or 0)

        lines = []
        lines.append("# TYPE kirogate_inflight_total gauge")
        lines.append(f"kirogate_inflight_total {total}")
        lines.append("# TYPE kirogate_inflight_by_phase gauge")
        for phase, n in by_phase.items():
            lines.append(
                f'kirogate_inflight_by_phase{{phase="{phase}"}} {n}'
            )
        lines.append("# TYPE kirogate_inflight_stalled gauge")
        lines.append(f"kirogate_inflight_stalled {stalled}")
        lines.append("# TYPE kirogate_inflight_max_age_seconds gauge")
        lines.append(f"kirogate_inflight_max_age_seconds {max_age:.3f}")
        lines.append("# TYPE kirogate_inflight_max_idle_seconds gauge")
        lines.append(f"kirogate_inflight_max_idle_seconds {max_idle:.3f}")
        if per_acct_bytes:
            lines.append("# TYPE kirogate_inflight_bytes_total gauge")
            for a, b in per_acct_bytes.items():
                lines.append(
                    f'kirogate_inflight_bytes_total{{account="{a}"}} {b}'
                )
            lines.append("# TYPE kirogate_inflight_events_total gauge")
            for a, e in per_acct_events.items():
                lines.append(
                    f'kirogate_inflight_events_total{{account="{a}"}} {e}'
                )
        return "\n".join(lines) + "\n"


class StallReaper:
    """Background sweeper that force-cancels zombies that beat the deadline.

    The reaper only fires the *cancel callback*; it never touches the
    in-flight stream directly, because the stream itself already raises
    StreamStalled via the watchdog inside `guarded_stream()`. This
    class exists for the rare case where the inner watchdog is
    somehow bypassed (e.g. the coroutine is blocked in C code that
    doesn't check cancellation).
    """

    def __init__(self, registry: InflightRegistry, interval: float = 5.0) -> None:
        self._reg = registry
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="kirogate-stall-reaper")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._interval
                    )
                    return  # stop was set
                except asyncio.TimeoutError:
                    pass
                await self._sweep()
        except asyncio.CancelledError:
            pass

    async def _sweep(self) -> None:
        now = time.time()
        handles = await self._reg.list_handles()
        for h in handles:
            if h.phase in ("done", "error", "stalled"):
                continue
            if now >= h.deadline_at:
                log.warning(
                    "reaper cancelling zombie request_id=%s age=%.1fs idle=%.1fs",
                    h.request_id, now - h.started_at, now - h.last_activity_at,
                )
                h.phase = "stalled"
                h.error = "deadline_reaped"
                if h._cancel_cb is not None:
                    try:
                        h._cancel_cb()
                    except Exception as e:  # noqa: BLE001
                        log.debug("cancel_cb raised: %s", e)


# ------------------------------------------------------------------ guarded stream


class ClientDisconnect:
    """Pollable wrapper around a FastAPI/Starlette Request.

    Usage:
        disc = ClientDisconnect(request)
        async for chunk in guarded_stream(..., cancel_if=disc.is_disconnected):
            ...

    If the client walks away mid-stream, the next watchdog tick will
    see is_disconnected() == True and cancel the upstream. The
    `is_disconnected()` call is cached for `poll_interval` so we don't
    burn CPU asking the ASGI server every tick.
    """

    def __init__(self, request: Any, poll_interval: float = 1.0) -> None:
        self._request = request
        self._poll_interval = poll_interval
        self._last_check = 0.0
        self._last_result = False

    async def is_disconnected(self) -> bool:
        if self._request is None:
            return False
        now = time.time()
        if now - self._last_check < self._poll_interval:
            return self._last_result
        self._last_check = now
        try:
            # Starlette Request exposes is_disconnected() as coroutine.
            val = self._request.is_disconnected()
            if asyncio.iscoroutine(val):
                self._last_result = bool(await val)
            else:
                self._last_result = bool(val)
        except Exception:  # noqa: BLE001
            # Unknown request shape: be conservative, say "still here".
            self._last_result = False
        return self._last_result


async def guarded_stream(
    response: httpx.Response,
    *,
    cfg: StallGuardConfig,
    handle: Optional[InflightHandle] = None,
    cancel_if: Optional[Callable[[], Awaitable[bool]]] = None,
    chunk_size: Optional[int] = None,
) -> AsyncIterator[bytes]:
    """Wrap `response.aiter_bytes()` with all six watchdogs.

    Raises:
        StreamStalled: if the first byte doesn't arrive in ttfb_timeout
            or a subsequent silence exceeds stream_idle_timeout.
        DeadlineExceeded: if total elapsed time passes total_deadline.
        ClientGone: if `cancel_if()` ever returns True.

    In all cases the underlying httpx response is closed before we
    re-raise, so there are no leaked connections.
    """
    started = time.time()
    deadline = started + cfg.total_deadline
    first_byte_seen = False

    # httpx's aiter_bytes returns an async generator; we read from it
    # via asyncio.wait_for to enforce our per-chunk deadlines.
    if chunk_size is None:
        source = response.aiter_bytes()
    else:
        source = response.aiter_bytes(chunk_size=chunk_size)
    ait = source.__aiter__()

    if handle is not None:
        handle.phase = "connected"
        handle.touch()

    close_done = False
    try:
        while True:
            now = time.time()
            if now >= deadline:
                raise DeadlineExceeded(
                    f"request exceeded total deadline of {cfg.total_deadline:.0f}s",
                    phase=handle.phase if handle else "unknown",
                    waited=now - started,
                )

            if cancel_if is not None:
                try:
                    if await cancel_if():
                        raise ClientGone("downstream client disconnected")
                except ClientGone:
                    raise
                except Exception:  # noqa: BLE001
                    pass  # never let disconnect-check itself break the stream

            # Pick the watchdog: TTFB vs mid-stream.
            if not first_byte_seen:
                per_chunk_timeout = min(cfg.ttfb_timeout, deadline - now)
                phase_label = "ttfb"
            else:
                per_chunk_timeout = min(cfg.stream_idle_timeout, deadline - now)
                phase_label = "idle"

            if per_chunk_timeout <= 0:
                raise DeadlineExceeded(
                    f"no time left for next chunk",
                    phase=handle.phase if handle else "unknown",
                    waited=now - started,
                )

            try:
                chunk = await asyncio.wait_for(
                    ait.__anext__(), timeout=per_chunk_timeout
                )
            except StopAsyncIteration:
                if handle is not None:
                    handle.phase = "done"
                    handle.touch()
                break
            except asyncio.TimeoutError:
                waited = time.time() - (handle.last_activity_at if handle else started)
                raise StreamStalled(
                    f"no bytes for {waited:.1f}s during {phase_label} phase",
                    phase=phase_label,
                    waited=waited,
                )

            if not first_byte_seen:
                first_byte_seen = True
                if handle is not None:
                    handle.phase = "streaming"

            if handle is not None:
                handle.touch(bytes_delta=len(chunk))

            yield chunk

    except BaseException as exc:
        # Always close the upstream on any exit path (including GeneratorExit
        # from consumer cancellation). This prevents pool leaks.
        with contextlib.suppress(Exception):
            await response.aclose()
            close_done = True
        if handle is not None and handle.phase not in ("done",):
            if handle.error is None:
                # Capture a concrete, inspectable error message on the
                # handle so /pool/inflight and kiro_inflight.py show the
                # real reason, not a useless placeholder.
                if isinstance(exc, StreamStalled):
                    handle.error = str(exc) or "stalled"
                elif isinstance(exc, ClientGone):
                    handle.error = "client_disconnected"
                elif isinstance(exc, GeneratorExit):
                    # Consumer walked away mid-iteration. This is the
                    # normal shutdown path for a FastAPI streaming
                    # response when the client disconnects; don't
                    # misclassify as "error".
                    handle.error = None
                else:
                    handle.error = f"{type(exc).__name__}: {str(exc)[:120]}"
            if handle.phase != "stalled":
                # GeneratorExit just means "consumer done, no error";
                # leave phase=streaming so it's not flagged as failed.
                if not isinstance(exc, GeneratorExit):
                    handle.phase = "error"
        raise
    finally:
        if not close_done:
            with contextlib.suppress(Exception):
                await response.aclose()


# ------------------------------------------------------------------ event counter helper


def count_event(handle: Optional[InflightHandle]) -> None:
    """Call from the SSE decoder every time a decoded event is emitted."""
    if handle is not None:
        handle.touch(events_delta=1)


# ------------------------------------------------------------------ selftest


async def _selftest() -> None:  # pragma: no cover
    """Standalone behavioural test — no network required."""
    # 1) A fake stream that produces two chunks then hangs.
    class _FakeResp:
        def __init__(self, chunks, hang_after=None):
            self._chunks = list(chunks)
            self._hang_after = hang_after
            self._closed = False

        async def aiter_bytes(self, chunk_size=None):
            for i, c in enumerate(self._chunks):
                yield c
            if self._hang_after is not None:
                # Simulate indefinite hang AFTER the provided chunks.
                await asyncio.sleep(100)

        async def aclose(self):
            self._closed = True

    cfg = StallGuardConfig(
        ttfb_timeout=1.0, stream_idle_timeout=1.0,
        total_deadline=10.0, socket_read=5.0,
    )
    reg = InflightRegistry()
    deadline = time.time() + cfg.total_deadline

    # --- test 1: completes cleanly
    async with reg.record(model="m", account="a", deadline_at=deadline) as h:
        resp = _FakeResp([b"hello", b"world"])
        got = []
        async for chunk in guarded_stream(resp, cfg=cfg, handle=h):
            got.append(chunk)
        assert got == [b"hello", b"world"], got
        assert h.phase == "done", h.phase
        assert h.bytes_received == 10, h.bytes_received
    assert resp._closed

    # --- test 2: hangs mid-stream -> StreamStalled
    async with reg.record(model="m", account="a", deadline_at=deadline) as h:
        resp = _FakeResp([b"hi", b"yo"], hang_after=2)
        try:
            async for chunk in guarded_stream(resp, cfg=cfg, handle=h):
                pass
        except StreamStalled as e:
            # Good
            assert e.phase in ("idle", "ttfb"), e.phase
            assert isinstance(e, httpx.HTTPError)
            assert getattr(e, "is_timeout", False) is True
        else:
            assert False, "expected StreamStalled"
        assert h.phase == "stalled" or h.phase == "error", h.phase
    assert resp._closed

    # --- test 3: downstream disconnect -> ClientGone
    cfg2 = StallGuardConfig(ttfb_timeout=2, stream_idle_timeout=2, total_deadline=10)
    async with reg.record(model="m", account="a", deadline_at=time.time() + 10) as h:
        resp = _FakeResp([b"chunk1", b"chunk2", b"chunk3"])
        tripped = {"v": False}

        async def disc():
            return tripped["v"]

        got = []
        try:
            async for chunk in guarded_stream(resp, cfg=cfg2, handle=h, cancel_if=disc):
                got.append(chunk)
                if len(got) == 1:
                    tripped["v"] = True
        except ClientGone:
            pass
        else:
            assert False, "expected ClientGone"
    assert resp._closed

    # --- test 4: deadline exceeded
    cfg3 = StallGuardConfig(ttfb_timeout=60, stream_idle_timeout=60, total_deadline=0.3)
    async with reg.record(model="m", account="a", deadline_at=time.time() + 0.3) as h:
        class _SlowResp:
            def __init__(self):
                self._closed = False

            async def aiter_bytes(self, chunk_size=None):
                # Produce one chunk fast, then sleep.
                yield b"ok"
                await asyncio.sleep(5)
                yield b"never"

            async def aclose(self):
                self._closed = True

        resp = _SlowResp()
        try:
            async for chunk in guarded_stream(resp, cfg=cfg3, handle=h):
                pass
        except (DeadlineExceeded, StreamStalled) as e:
            # Either is acceptable — deadline wins if it fires first.
            pass
        else:
            assert False, "expected deadline"
        assert resp._closed

    # --- test 5: registry snapshot/cancel callback
    cancelled = {"v": False}

    def _cancel():
        cancelled["v"] = True

    reg2 = InflightRegistry()
    async with reg2.record(
        model="m", account="a", deadline_at=time.time() + 0.1, cancel_cb=_cancel
    ) as h:
        snap = await reg2.snapshot()
        assert len(snap) == 1 and snap[0]["model"] == "m"
        assert await reg2.count() == 1
        reaper = StallReaper(reg2, interval=0.05)
        await reaper.start()
        await asyncio.sleep(0.25)
        await reaper.stop()
    assert cancelled["v"], "reaper should have fired cancel_cb"

    print("stall_guard selftest OK")


if __name__ == "__main__":
    asyncio.run(_selftest())
