#!/usr/bin/env python3
"""kiro_inflight.py — real-time dashboard of in-flight kirogate requests.

Polls GET /pool/inflight on a running kirogate gateway and renders a
curses-style table that refreshes every N seconds. Lets the contractor
("包工头") see which Kiro calls are really making progress and which
ones are zombies stuck in TTFB or idle forever.

Usage:
    # Basic (auto-refresh every 1s until Ctrl+C):
    python kiro_inflight.py --url http://localhost:8787

    # Specify the gateway's admin key (same one you use for
    # Authorization: Bearer):
    python kiro_inflight.py --url http://localhost:8787 --key sk_abc...

    # Single snapshot, JSON (for cron / alerts):
    python kiro_inflight.py --url http://localhost:8787 --once --json

    # Alert-mode: exit 1 if any request is stalled.
    python kiro_inflight.py --url http://localhost:8787 --once \
        --max-idle 60 --max-age 600

The corresponding endpoint on kirogate:
    @app.get("/pool/inflight")
    async def inflight(): return await app.state.inflight.snapshot()
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

import httpx


def _fmt_sec(s: Optional[float]) -> str:
    if s is None:
        return "-"
    if s < 0:
        return f"-{-s:.0f}s"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s//60)}m{int(s%60):02d}s"
    return f"{int(s//3600)}h{int((s%3600)//60):02d}m"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}K"
    return f"{n/1024/1024:.2f}M"


def _phase_color(phase: str, ansi: bool) -> str:
    if not ansi:
        return phase
    colors = {
        "pending": "\033[33m",    # yellow
        "connected": "\033[36m",  # cyan
        "streaming": "\033[32m",  # green
        "done": "\033[90m",       # grey
        "error": "\033[31m",      # red
        "stalled": "\033[31;1m",  # bold red
    }
    reset = "\033[0m"
    return f"{colors.get(phase, '')}{phase}{reset}"


def render_table(rows: List[Dict[str, Any]], *, ansi: bool = True) -> str:
    if not rows:
        return "(no in-flight requests)"
    width = shutil.get_terminal_size((140, 24)).columns
    # Columns: req_id(13) account(16) model(25) phase(12) age(7) idle(7) rem(7) bytes(7) events(6) error
    hdr = (
        f"{'REQ':<13} {'ACCOUNT':<16} {'MODEL':<25} {'PHASE':<12} "
        f"{'AGE':>7} {'IDLE':>7} {'REM':>7} {'BYTES':>7} {'EVENTS':>6}  NOTE"
    )
    out = [hdr, "-" * min(len(hdr), width)]
    for r in rows:
        idle = r.get("idle_sec", 0)
        phase = r.get("phase", "?")
        # Flag suspicious rows ourselves (independent of server classification).
        flag = ""
        if phase in ("error", "stalled"):
            flag = "!!"
        elif idle > 10 and phase == "streaming":
            flag = "?!"
        elif r.get("remaining_sec", 1) < 0:
            flag = "!!"
        note_parts = [flag] if flag else []
        if r.get("error"):
            note_parts.append(str(r["error"])[:40])
        note = " ".join(note_parts)

        line = (
            f"{str(r.get('request_id', '-'))[:13]:<13} "
            f"{str(r.get('account', '-'))[:16]:<16} "
            f"{str(r.get('model', '-'))[:25]:<25} "
            f"{_phase_color(phase, ansi):<{12 + (len(_phase_color(phase, ansi)) - len(phase) if ansi else 0)}} "
            f"{_fmt_sec(r.get('age_sec')):>7} "
            f"{_fmt_sec(idle):>7} "
            f"{_fmt_sec(r.get('remaining_sec')):>7} "
            f"{_fmt_bytes(r.get('bytes', 0)):>7} "
            f"{r.get('events', 0):>6}  "
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
        phase = r.get("phase", "?")
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if phase in ("error", "stalled"):
            stalled += 1
        elif phase in ("streaming", "connected", "pending"):
            healthy += 1
        max_age = max(max_age, r.get("age_sec", 0) or 0)
        max_idle = max(max_idle, r.get("idle_sec", 0) or 0)
    return {
        "total": total, "healthy": healthy, "stalled": stalled,
        "by_phase": by_phase, "max_age_sec": max_age, "max_idle_sec": max_idle,
    }


def poll_once(url: str, key: Optional[str], timeout: float = 5.0) -> List[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url.rstrip("/") + "/pool/inflight", headers=headers)
        r.raise_for_status()
        data = r.json()
    # Accept either a bare list or {"inflight":[...]} wrapped response.
    if isinstance(data, dict):
        for k in ("inflight", "items", "rows"):
            if k in data and isinstance(data[k], list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--url", default=os.environ.get("KIROGATE_URL", "http://localhost:8787"))
    p.add_argument("--key", default=os.environ.get("KIROGATE_KEY"))
    p.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--max-idle", type=float, default=None,
                   help="with --once: exit 1 if any request has idle >= N seconds")
    p.add_argument("--max-age", type=float, default=None,
                   help="with --once: exit 1 if any request has age >= N seconds")
    p.add_argument("--alert-any-stalled", action="store_true",
                   help="with --once: exit 1 if any phase is stalled/error")
    return p


def main() -> int:
    args = build_parser().parse_args()
    ansi = sys.stdout.isatty() and not args.no_color

    def _render_frame() -> int:
        try:
            rows = poll_once(args.url, args.key)
        except httpx.HTTPError as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"ERROR: {e}", file=sys.stderr)
            return 2

        summary = summarize(rows)

        if args.json:
            print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        else:
            sys.stdout.write("\033[2J\033[H" if ansi else "")
            sys.stdout.write(
                f"kirogate /pool/inflight @ {args.url}  "
                f"total={summary['total']}  "
                f"healthy={summary['healthy']}  "
                f"stalled={summary['stalled']}  "
                f"max_age={_fmt_sec(summary['max_age_sec'])}  "
                f"max_idle={_fmt_sec(summary['max_idle_sec'])}\n"
            )
            print(render_table(rows, ansi=ansi))
            sys.stdout.flush()

        # Alert checks (only in --once mode).
        if args.once:
            if args.alert_any_stalled and summary["stalled"] > 0:
                return 1
            if args.max_idle is not None and summary["max_idle_sec"] >= args.max_idle:
                return 1
            if args.max_age is not None and summary["max_age_sec"] >= args.max_age:
                return 1
        return 0

    if args.once:
        return _render_frame()

    try:
        while True:
            _render_frame()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
