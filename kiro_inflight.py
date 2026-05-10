#!/usr/bin/env python3
"""kiro_inflight.py — real-time dashboard of in-flight kirogate requests.

Polls GET /pool/inflight on a running kirogate gateway and renders a
curses-style table that refreshes every N seconds. Lets the contractor
("包工头") see which Kiro calls are really making progress and which
ones are zombies stuck in TTFB or idle forever.

Usage:
    # Basic (auto-refresh every 1s until Ctrl+C)
    python kiro_inflight.py --url http://localhost:8787

    # Gateway admin key
    python kiro_inflight.py --url http://localhost:8787 --key sk_abc...

    # Sort by idle descending so zombies float to the top
    python kiro_inflight.py --sort idle --desc

    # Filter to a specific account or model
    python kiro_inflight.py --account acct_a
    python kiro_inflight.py --model claude-opus

    # Single JSON snapshot for cron / Prometheus
    python kiro_inflight.py --once --json

    # Prometheus text exposition
    python kiro_inflight.py --once --prometheus

    # Alert mode (for pagers): exit 1 if any request is stalled,
    # idle longer than 60s, or older than 600s.
    python kiro_inflight.py --once \
        --alert-any-stalled --max-idle 60 --max-age 600

The corresponding endpoint on kirogate:
    @app.get("/pool/inflight")
    async def inflight(): return await app.state.inflight.snapshot()
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

import httpx


# ------------------------------------------------------------------ formatting


def _fmt_sec(s: Optional[float]) -> str:
    if s is None:
        return "-"
    if s < 0:
        return f"-{-s:.0f}s"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int((s % 3600) // 60):02d}m"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / 1024 / 1024:.2f}M"


_PHASE_COLORS = {
    "pending": "\033[33m",    # yellow
    "connected": "\033[36m",  # cyan
    "streaming": "\033[32m",  # green
    "done": "\033[90m",       # grey
    "error": "\033[31m",      # red
    "stalled": "\033[31;1m",  # bold red
}
_ANSI_RESET = "\033[0m"


def _pad_with_ansi(s: str, visible_width: int, visible_len: int) -> str:
    """Right-pad a string containing ANSI escape codes so its visible
    portion lines up to `visible_width` columns."""
    pad = max(0, visible_width - visible_len)
    return s + " " * pad


def _colored_phase(phase: str, ansi: bool, width: int) -> str:
    if not ansi or phase not in _PHASE_COLORS:
        return f"{phase:<{width}}"
    colored = f"{_PHASE_COLORS[phase]}{phase}{_ANSI_RESET}"
    return _pad_with_ansi(colored, width, len(phase))


# ------------------------------------------------------------------ render


_SORT_KEYS = {
    "idle": lambda r: -float(r.get("idle_sec") or 0),         # desc by default
    "age": lambda r: -float(r.get("age_sec") or 0),
    "bytes": lambda r: -int(r.get("bytes") or 0),
    "events": lambda r: -int(r.get("events") or 0),
    "account": lambda r: str(r.get("account") or ""),
    "model": lambda r: str(r.get("model") or ""),
    "phase": lambda r: str(r.get("phase") or ""),
}


def _filter_rows(
    rows: List[Dict[str, Any]],
    *,
    account: Optional[str] = None,
    model: Optional[str] = None,
    phase: Optional[str] = None,
    min_idle: Optional[float] = None,
) -> List[Dict[str, Any]]:
    def keep(r: Dict[str, Any]) -> bool:
        if account and account not in str(r.get("account") or ""):
            return False
        if model and model not in str(r.get("model") or ""):
            return False
        if phase and phase != str(r.get("phase") or ""):
            return False
        if min_idle is not None and float(r.get("idle_sec") or 0) < min_idle:
            return False
        return True

    return [r for r in rows if keep(r)]


def _sort_rows(
    rows: List[Dict[str, Any]],
    sort_key: Optional[str],
    descending: bool,
) -> List[Dict[str, Any]]:
    if not sort_key or sort_key not in _SORT_KEYS:
        return rows
    keyfn = _SORT_KEYS[sort_key]
    # The built-in "idle/age/bytes/events" keyfn already negates for desc;
    # undo that if user asked for ascending order.
    if sort_key in ("idle", "age", "bytes", "events") and not descending:
        real = lambda r: -keyfn(r)  # noqa: E731
        return sorted(rows, key=real)
    if sort_key in ("idle", "age", "bytes", "events"):
        # keyfn is already "desc" — so ascending flag is False here,
        # descending=True means use keyfn as-is (which is desc).
        return sorted(rows, key=keyfn)
    # For string keys, sorted() is ascending by default.
    out = sorted(rows, key=keyfn)
    if descending:
        out.reverse()
    return out


def render_table(rows: List[Dict[str, Any]], *, ansi: bool = True) -> str:
    if not rows:
        return "(no in-flight requests)"
    # Columns: req_id(13) account(16) model(25) phase(12) age(7) idle(7) rem(7) bytes(7) events(6) note
    hdr = (
        f"{'REQ':<13} {'ACCOUNT':<16} {'MODEL':<25} {'PHASE':<12} "
        f"{'AGE':>7} {'IDLE':>7} {'REM':>7} {'BYTES':>7} {'EVENTS':>6}  NOTE"
    )
    out = [hdr, "-" * len(hdr)]
    for r in rows:
        idle = float(r.get("idle_sec") or 0)
        phase = str(r.get("phase") or "?")
        # Independent flagging (robust even if server misses a case).
        flag = ""
        if phase in ("error", "stalled"):
            flag = "!!"
        elif idle > 10 and phase == "streaming":
            flag = "?!"
        elif float(r.get("remaining_sec") or 1) < 0:
            flag = "!!"
        note_parts = [flag] if flag else []
        if r.get("error"):
            note_parts.append(str(r["error"])[:60])
        note = " ".join(note_parts)

        line = (
            f"{str(r.get('request_id', '-'))[:13]:<13} "
            f"{str(r.get('account', '-'))[:16]:<16} "
            f"{str(r.get('model', '-'))[:25]:<25} "
            f"{_colored_phase(phase, ansi, 12)} "
            f"{_fmt_sec(r.get('age_sec')):>7} "
            f"{_fmt_sec(idle):>7} "
            f"{_fmt_sec(r.get('remaining_sec')):>7} "
            f"{_fmt_bytes(int(r.get('bytes') or 0)):>7} "
            f"{int(r.get('events') or 0):>6}  "
            f"{note}"
        )
        out.append(line)
    return "\n".join(out)


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    healthy = 0
    stalled = 0
    max_age = 0.0
    max_idle = 0.0
    by_phase: Dict[str, int] = {}
    for r in rows:
        phase = str(r.get("phase") or "?")
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if phase in ("error", "stalled"):
            stalled += 1
        elif phase in ("streaming", "connected", "pending"):
            healthy += 1
        max_age = max(max_age, float(r.get("age_sec") or 0))
        max_idle = max(max_idle, float(r.get("idle_sec") or 0))
    return {
        "total": total,
        "healthy": healthy,
        "stalled": stalled,
        "by_phase": by_phase,
        "max_age_sec": max_age,
        "max_idle_sec": max_idle,
    }


# ------------------------------------------------------------------ prometheus


_PROM_METRIC = re.compile(r"[^a-zA-Z0-9_]")


def _prom_label(s: str) -> str:
    return _PROM_METRIC.sub("_", str(s))


def render_prometheus(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    """Minimal Prometheus text-format exposition."""
    out = []
    out.append("# HELP kirogate_inflight_total Current in-flight request count.")
    out.append("# TYPE kirogate_inflight_total gauge")
    out.append(f"kirogate_inflight_total {summary['total']}")

    out.append("# HELP kirogate_inflight_by_phase In-flight requests grouped by phase.")
    out.append("# TYPE kirogate_inflight_by_phase gauge")
    for phase, n in summary.get("by_phase", {}).items():
        out.append(f'kirogate_inflight_by_phase{{phase="{_prom_label(phase)}"}} {n}')

    out.append("# HELP kirogate_inflight_max_age_seconds Age of oldest in-flight request.")
    out.append("# TYPE kirogate_inflight_max_age_seconds gauge")
    out.append(f"kirogate_inflight_max_age_seconds {summary['max_age_sec']:.3f}")

    out.append("# HELP kirogate_inflight_max_idle_seconds Longest silence of any in-flight request.")
    out.append("# TYPE kirogate_inflight_max_idle_seconds gauge")
    out.append(f"kirogate_inflight_max_idle_seconds {summary['max_idle_sec']:.3f}")

    out.append("# HELP kirogate_inflight_stalled Number of requests currently classified as stalled.")
    out.append("# TYPE kirogate_inflight_stalled gauge")
    out.append(f"kirogate_inflight_stalled {summary['stalled']}")

    # Per-account bytes/events so you can graph throughput distribution.
    per_acct_bytes: Dict[str, int] = {}
    per_acct_events: Dict[str, int] = {}
    for r in rows:
        acct = _prom_label(r.get("account") or "unknown")
        per_acct_bytes[acct] = per_acct_bytes.get(acct, 0) + int(r.get("bytes") or 0)
        per_acct_events[acct] = per_acct_events.get(acct, 0) + int(r.get("events") or 0)

    if per_acct_bytes:
        out.append("# HELP kirogate_inflight_bytes_total Bytes received per account across live requests.")
        out.append("# TYPE kirogate_inflight_bytes_total gauge")
        for a, b in per_acct_bytes.items():
            out.append(f'kirogate_inflight_bytes_total{{account="{a}"}} {b}')
        out.append("# HELP kirogate_inflight_events_total Events decoded per account across live requests.")
        out.append("# TYPE kirogate_inflight_events_total gauge")
        for a, e in per_acct_events.items():
            out.append(f'kirogate_inflight_events_total{{account="{a}"}} {e}')

    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ transport


class _Poller:
    """Reuses a single httpx.Client across poll ticks.

    Previously we opened a new Client per tick, which did a fresh TCP
    handshake every second. Now the connection is kept alive.
    """

    def __init__(
        self,
        url: str,
        key: Optional[str],
        *,
        timeout: float = 5.0,
    ) -> None:
        self._url = url.rstrip("/") + "/pool/inflight"
        headers: Dict[str, str] = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def poll(self) -> List[Dict[str, Any]]:
        r = self._client.get(self._url)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            for k in ("inflight", "items", "rows"):
                if k in data and isinstance(data[k], list):
                    return data[k]
            return []
        return data if isinstance(data, list) else []

    def close(self) -> None:
        self._client.close()


# ------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--url", default=os.environ.get("KIROGATE_URL", "http://localhost:8787"))
    p.add_argument("--key", default=os.environ.get("KIROGATE_KEY"))
    p.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--prometheus", action="store_true", help="Prometheus text exposition output")
    p.add_argument("--no-color", action="store_true")
    # Sort/filter knobs.
    p.add_argument("--sort", choices=sorted(_SORT_KEYS.keys()),
                   default=os.environ.get("KIROGATE_INFLIGHT_SORT", "idle"),
                   help="sort column (default: idle, desc)")
    p.add_argument("--asc", action="store_true",
                   help="sort ascending (default is descending for idle/age/bytes/events)")
    p.add_argument("--account", help="substring filter on account name")
    p.add_argument("--model", help="substring filter on model name")
    p.add_argument("--phase", help="exact match filter on phase")
    p.add_argument("--min-idle", type=float, help="only show rows with idle_sec >= N")
    # Alert thresholds (only apply with --once).
    p.add_argument("--max-idle", type=float, default=None,
                   help="with --once: exit 1 if any idle >= N seconds")
    p.add_argument("--max-age", type=float, default=None,
                   help="with --once: exit 1 if any age >= N seconds")
    p.add_argument("--alert-any-stalled", action="store_true",
                   help="with --once: exit 1 if any phase is stalled/error")
    return p


def _prepare_rows(
    rows: List[Dict[str, Any]], args: argparse.Namespace
) -> List[Dict[str, Any]]:
    rows = _filter_rows(
        rows,
        account=args.account,
        model=args.model,
        phase=args.phase,
        min_idle=args.min_idle,
    )
    rows = _sort_rows(rows, args.sort, descending=not args.asc)
    return rows


def main() -> int:
    args = build_parser().parse_args()
    ansi = sys.stdout.isatty() and not args.no_color
    if args.json and args.prometheus:
        print("--json and --prometheus are mutually exclusive", file=sys.stderr)
        return 2

    poller = _Poller(args.url, args.key)

    def _render_frame() -> int:
        try:
            raw_rows = poller.poll()
        except httpx.HTTPError as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"ERROR: {e}", file=sys.stderr)
            return 2

        rows = _prepare_rows(raw_rows, args)
        summary = summarize(rows)

        if args.json:
            print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        elif args.prometheus:
            print(render_prometheus(rows, summary))
        else:
            if ansi:
                sys.stdout.write("\033[2J\033[H")
            filters = []
            if args.account:
                filters.append(f"account~{args.account}")
            if args.model:
                filters.append(f"model~{args.model}")
            if args.phase:
                filters.append(f"phase={args.phase}")
            if args.min_idle:
                filters.append(f"idle>={args.min_idle}s")
            filter_str = ("  filter=[" + ",".join(filters) + "]") if filters else ""
            sort_str = f"  sort={args.sort}{'↑' if args.asc else '↓'}"
            sys.stdout.write(
                f"kirogate /pool/inflight @ {args.url}  "
                f"shown={summary['total']}  "
                f"healthy={summary['healthy']}  "
                f"stalled={summary['stalled']}  "
                f"max_age={_fmt_sec(summary['max_age_sec'])}  "
                f"max_idle={_fmt_sec(summary['max_idle_sec'])}"
                f"{sort_str}{filter_str}\n"
            )
            print(render_table(rows, ansi=ansi))
            sys.stdout.flush()

        if args.once:
            if args.alert_any_stalled and summary["stalled"] > 0:
                return 1
            if args.max_idle is not None and summary["max_idle_sec"] >= args.max_idle:
                return 1
            if args.max_age is not None and summary["max_age_sec"] >= args.max_age:
                return 1
        return 0

    try:
        if args.once:
            return _render_frame()
        while True:
            _render_frame()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        poller.close()


if __name__ == "__main__":
    sys.exit(main())
