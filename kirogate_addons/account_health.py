"""Preflight health-check CLI for a kirogate key pool.

What it does
------------
For every `ksk_` key you give it, probes:
    1. GetUsageLimits   — are you authenticated? what's the balance?
    2. ListAvailableModels — can the key actually read the model catalog?
    3. A tiny GenerateAssistantResponse call (opt-in, --probe)
       using qwen3-coder-next to verify streaming works end-to-end.

Outputs a single table you can paste anywhere + non-zero exit code if
any key is unhealthy, so you can use it in CI / pre-deploy hooks:

    python -m kirogate_addons.account_health --file accounts.txt || exit 1

Classification
--------------
  HEALTHY       — passes all selected probes, remaining >= warn_threshold
  LOW_CREDITS   — passes probes but remaining < warn_threshold
  THROTTLED     — auth OK, but getting 429 / ThrottlingException
  INVALID       — 403 AccessDeniedException (wrong key, revoked, missing
                  tokentype header)
  NETWORK       — transient failure; retry later
  UNKNOWN       — other

Exit codes
----------
  0   all keys HEALTHY
  1   at least one key not HEALTHY (including LOW_CREDITS)
  2   bad arguments
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

# Try to import the sibling quota_monitor for zero-duplication; fall
# back to a local implementation so this script is usable standalone.
try:
    from .quota_monitor import fetch_usage_limits, _parse_usage_limits, _preview  # type: ignore
except Exception:  # pragma: no cover - standalone mode
    from quota_monitor import fetch_usage_limits, _parse_usage_limits, _preview  # type: ignore


KIRO_HOST = "https://q.us-east-1.amazonaws.com"
_UA = (
    "aws-sdk-rust/1.3.14 ua/2.1 api/codewhispererstreaming/0.1.14474 "
    "os/linux lang/rust/1.92.0 m/F app/AmazonQ-Developer-CLI/2.2.2"
)


def _hdrs(api_key: str, target: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": target,
        "User-Agent": _UA,
        "x-amz-user-agent": _UA,
        "tokentype": "API_KEY",
        "accept": "*/*",
    }


async def _list_models(http: httpx.AsyncClient, api_key: str) -> Dict[str, Any]:
    r = await http.post(
        KIRO_HOST + "/",
        headers=_hdrs(api_key, "AmazonCodeWhispererService.ListAvailableModels"),
        params={"origin": "KIRO_CLI"},
        content=b'{"origin":"KIRO_CLI"}',
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


async def _probe_generate(http: httpx.AsyncClient, api_key: str) -> float:
    """Single cheapest-model call. Returns credit cost. Raises on failure."""
    # Local eventstream decoder (duplicated to keep file standalone).
    import struct

    class _Dec:
        def __init__(self) -> None:
            self.buf = bytearray()

        def feed(self, data: bytes):
            self.buf.extend(data)
            out = []
            while len(self.buf) >= 12:
                tl, hl = struct.unpack(">II", bytes(self.buf[:8]))
                if tl < 16 or hl > tl or len(self.buf) < tl:
                    if tl < 16 or hl > tl:
                        del self.buf[:1]
                        continue
                    break
                frame = bytes(self.buf[:tl]); del self.buf[:tl]
                headers_raw = frame[12 : 12 + hl]
                payload = frame[12 + hl : tl - 4]
                hdrs = {}
                i = 0; n = len(headers_raw)
                while i < n:
                    nl = headers_raw[i]; i += 1
                    name = headers_raw[i : i + nl].decode("utf-8", "replace"); i += nl
                    vt = headers_raw[i]; i += 1
                    if vt == 7:
                        vl = struct.unpack(">H", headers_raw[i : i + 2])[0]; i += 2
                        hdrs[name] = headers_raw[i : i + vl].decode("utf-8", "replace"); i += vl
                    else:
                        break
                out.append((hdrs, payload))
            return out

    payload = {
        "conversationState": {
            "conversationId": str(uuid.uuid4()),
            "history": [],
            "currentMessage": {
                "userInputMessage": {
                    "content": (
                        "--- CONTEXT ENTRY BEGIN ---\n--- CONTEXT ENTRY END ---\n\n"
                        "--- USER MESSAGE BEGIN ---\nReply with just: OK"
                        "--- USER MESSAGE END ---"
                    ),
                    "userInputMessageContext": {},
                    "origin": "KIRO_CLI",
                    "modelId": "qwen3-coder-next",
                }
            },
            "chatTriggerType": "MANUAL",
            "agentContinuationId": str(uuid.uuid4()),
            "agentTaskType": "vibe",
        }
    }
    target = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
    req = http.build_request(
        "POST", KIRO_HOST + "/",
        headers=_hdrs(api_key, target),
        content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    r = await http.send(req, stream=True)
    try:
        if r.status_code != 200:
            body = await r.aread()
            raise httpx.HTTPStatusError(
                f"{r.status_code}: {body.decode('utf-8', 'replace')[:200]}",
                request=req, response=r,
            )
        dec = _Dec()
        credits = 0.0
        async for chunk in r.aiter_bytes():
            for hdrs, pb in dec.feed(chunk):
                if hdrs.get(":event-type") == "meteringEvent" and pb:
                    credits += float(json.loads(pb.decode("utf-8")).get("usage", 0.0))
        return credits
    finally:
        await r.aclose()


# ---------------------------------------------------------------- report


HEALTHY = "HEALTHY"
LOW_CREDITS = "LOW_CREDITS"
THROTTLED = "THROTTLED"
INVALID = "INVALID"
NETWORK = "NETWORK"
UNKNOWN = "UNKNOWN"


@dataclass
class Finding:
    key_preview: str
    name: str
    status: str
    remaining: Optional[float] = None
    usage_limit: Optional[float] = None
    subscription: Optional[str] = None
    models_ok: Optional[bool] = None
    probe_credits: Optional[float] = None
    reset_at: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def is_healthy(self) -> bool:
        return self.status == HEALTHY


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = (exc.response.text or "").lower()
        if status in (401, 403):
            if "throttl" in body or "rate" in body:
                return THROTTLED
            return INVALID
        if status == 429:
            return THROTTLED
        if 500 <= status < 600:
            return NETWORK
        return UNKNOWN
    return NETWORK


async def audit_one(
    http: httpx.AsyncClient,
    key: str,
    name: str,
    *,
    warn_threshold: float,
    probe: bool,
) -> Finding:
    f = Finding(key_preview=_preview(key), name=name, status=UNKNOWN)

    # 1) GetUsageLimits
    try:
        data = await fetch_usage_limits(http, key)
        snap = _parse_usage_limits(key, data)
        f.remaining = snap.remaining
        f.usage_limit = snap.usage_limit
        f.subscription = snap.subscription_title
        if snap.next_reset_unix:
            f.reset_at = datetime.fromtimestamp(
                snap.next_reset_unix, tz=timezone.utc
            ).isoformat()
    except Exception as e:
        f.errors.append(f"GetUsageLimits: {e}")
        f.status = _classify_error(e)
        return f

    # 2) ListAvailableModels
    try:
        models = await _list_models(http, key)
        f.models_ok = bool(models.get("models"))
    except Exception as e:
        f.errors.append(f"ListAvailableModels: {e}")
        f.models_ok = False
        # Not immediately fatal — but downgrade status.
        if f.status == UNKNOWN:
            f.status = _classify_error(e)

    # 3) optional probe
    if probe:
        try:
            f.probe_credits = await _probe_generate(http, key)
        except Exception as e:
            f.errors.append(f"probe: {e}")
            if f.status == UNKNOWN:
                f.status = _classify_error(e)

    # Final classification
    if f.models_ok and not f.errors:
        if (
            f.remaining is not None
            and f.usage_limit is not None
            and f.remaining < warn_threshold
        ):
            f.status = LOW_CREDITS
        else:
            f.status = HEALTHY
    elif f.models_ok and probe and f.probe_credits is not None and not [
        e for e in f.errors if "probe" in e or "ListAvailableModels" in e
    ]:
        f.status = HEALTHY
    else:
        # Keep whatever _classify_error decided; else UNKNOWN
        if f.status == UNKNOWN and f.errors:
            f.status = NETWORK
    return f


# ---------------------------------------------------------------- rendering


_STATUS_ICON = {
    HEALTHY: "OK",
    LOW_CREDITS: "LO",
    THROTTLED: "TH",
    INVALID: "!!",
    NETWORK: "..",
    UNKNOWN: "??",
}


def _render_table(findings: List[Finding]) -> str:
    out = []
    header = (
        f"{'':<4}{'NAME':<14}{'KEY':<20}{'SUBSCRIPTION':<14}"
        f"{'REMAINING':>14}{'LIMIT':>10}{'MODELS':>8}{'PROBE':>10}  STATUS"
    )
    out.append(header)
    out.append("-" * len(header))
    for f in findings:
        rem = f"{f.remaining:.2f}" if f.remaining is not None else "-"
        lim = f"{f.usage_limit:.0f}" if f.usage_limit is not None else "-"
        mdl = "yes" if f.models_ok else "no " if f.models_ok is False else "-"
        pr = f"{f.probe_credits:.4f}" if f.probe_credits is not None else "-"
        sub = (f.subscription or "-")[:13]
        out.append(
            f"{_STATUS_ICON.get(f.status, '??'):<4}{f.name:<14}{f.key_preview:<20}"
            f"{sub:<14}{rem:>14}{lim:>10}{mdl:>8}{pr:>10}  {f.status}"
        )
        for err in f.errors:
            out.append(f"      err: {err[:140]}")
    return "\n".join(out)


# ---------------------------------------------------------------- main


def _load_keys(args: argparse.Namespace) -> List[tuple]:
    """Returns [(key, name), ...]."""
    result: List[tuple] = []
    idx = 0
    for k in args.keys:
        result.append((k, f"cli{idx}"))
        idx += 1
    if args.env:
        val = os.environ.get(args.env_var, "")
        for raw in val.split(","):
            raw = raw.strip()
            if raw:
                result.append((raw, f"env{idx}"))
                idx += 1
    if args.file:
        data = open(args.file, "r", encoding="utf-8").read()
        # Accept either plain one-per-line OR JSON (same format kirogate uses).
        data_stripped = data.strip()
        if data_stripped.startswith("["):
            try:
                parsed = json.loads(data_stripped)
                for item in parsed:
                    if isinstance(item, str):
                        result.append((item, f"file{idx}"))
                    elif isinstance(item, dict) and "key" in item:
                        result.append(
                            (item["key"], item.get("name") or f"file{idx}")
                        )
                    idx += 1
            except Exception as e:
                print(f"failed to parse {args.file} as JSON: {e}", file=sys.stderr)
                sys.exit(2)
        else:
            for line in data.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    result.append((line, f"file{idx}"))
                    idx += 1
    # dedupe by key, preserve order
    seen = set()
    out = []
    for k, n in result:
        if k not in seen:
            seen.add(k)
            out.append((k, n))
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="account_health",
        description="Preflight health check for kirogate ksk_ API keys.",
    )
    p.add_argument("keys", nargs="*", help="ksk_ keys directly on command line")
    p.add_argument("--env", action="store_true",
                   help="read keys from env var (default KIRO_API_KEYS)")
    p.add_argument("--env-var", default="KIRO_API_KEYS")
    p.add_argument("--file", help="read keys from a file (one per line or JSON)")
    p.add_argument("--probe", action="store_true",
                   help="also perform a tiny GenerateAssistantResponse call per key")
    p.add_argument("--warn-threshold", type=float, default=100.0,
                   help="remaining credits below this => LOW_CREDITS (default 100)")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of table")
    return p


async def amain() -> int:
    args = _build_parser().parse_args()
    keys = _load_keys(args)
    if not keys:
        print("No keys provided. Use positional args, --env, or --file.",
              file=sys.stderr)
        return 2

    async with httpx.AsyncClient() as http:
        sem = asyncio.Semaphore(args.concurrency)

        async def work(k, n):
            async with sem:
                return await audit_one(
                    http, k, n,
                    warn_threshold=args.warn_threshold,
                    probe=args.probe,
                )

        findings = await asyncio.gather(*(work(k, n) for k, n in keys))

    if args.json:
        print(json.dumps(
            [f.__dict__ for f in findings], indent=2, ensure_ascii=False
        ))
    else:
        print(_render_table(findings))
        total_remaining = sum((f.remaining or 0) for f in findings if f.remaining is not None)
        total_limit = sum((f.usage_limit or 0) for f in findings if f.usage_limit is not None)
        healthy = sum(1 for f in findings if f.status == HEALTHY)
        print()
        print(
            f"summary: {healthy}/{len(findings)} healthy | "
            f"aggregate remaining {total_remaining:.2f} / {total_limit:.0f} credits"
        )

    return 0 if all(f.is_healthy() for f in findings) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
