#!/usr/bin/env python3
"""kiro_quota.py — Query remaining quota for Kiro headless (ksk_) API keys.

Use this to audit every key in your rotation pool without burning credits.

Usage:
    # single key
    python kiro_quota.py ksk_XXXXXXXX

    # multiple keys
    python kiro_quota.py ksk_AAA ksk_BBB ksk_CCC

    # read keys from env KIRO_API_KEYS (comma separated)
    python kiro_quota.py --env

    # read keys from a file, one per line
    python kiro_quota.py --file keys.txt

    # also probe each key with a tiny live call to measure per-request cost
    python kiro_quota.py --probe ksk_XXXX

Output:
    Prints a table with:
        subscription tier, usage limit, current usage, remaining, reset date

    --probe additionally does a single cheapest-model call
    (qwen3-coder-next, rate=0.05) to sample the actual credit cost per
    request, then estimates how many more such calls this key can make.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx

KIRO_HOST = "https://q.us-east-1.amazonaws.com"
KIRO_ORIGIN = "KIRO_CLI"
USER_AGENT = (
    "aws-sdk-rust/1.3.14 ua/2.1 api/codewhispererstreaming/0.1.14474 "
    "os/linux lang/rust/1.92.0 m/F app/AmazonQ-Developer-CLI/2.2.2"
)


def _headers(api_key: str, target: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": target,
        "User-Agent": USER_AGENT,
        "x-amz-user-agent": USER_AGENT,
        "x-amzn-codewhisperer-optout": "false",
        "tokentype": "API_KEY",  # critical for ksk_ keys
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
        "accept": "*/*",
    }


@dataclass
class QuotaReport:
    key_preview: str
    ok: bool
    error: Optional[str] = None
    subscription_title: Optional[str] = None
    subscription_type: Optional[str] = None
    usage_limit: Optional[float] = None
    current_usage: Optional[float] = None
    overage_status: Optional[str] = None
    overage_cap: Optional[float] = None
    overage_rate: Optional[float] = None
    reset_at: Optional[str] = None
    user_id: Optional[str] = None
    # Optional probe result:
    probe_credits_per_call: Optional[float] = None
    probe_model: Optional[str] = None

    @property
    def remaining(self) -> Optional[float]:
        if self.usage_limit is None or self.current_usage is None:
            return None
        return max(self.usage_limit - self.current_usage, 0.0)


def _preview(key: str) -> str:
    if len(key) <= 16:
        return key
    return key[:10] + "…" + key[-4:]


async def fetch_usage_limits(client: httpx.AsyncClient, api_key: str) -> dict:
    r = await client.post(
        KIRO_HOST + "/",
        headers=_headers(api_key, "AmazonCodeWhispererService.GetUsageLimits"),
        content=b"{}",
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def probe_one_call(
    client: httpx.AsyncClient, api_key: str, model: str = "qwen3-coder-next"
) -> float:
    """Perform a single cheapest-model call; return credits charged for it."""
    from_kiroagate_eventstream = None  # noqa (placeholder if we used one)

    # Local minimal eventstream decoder
    import struct
    class _Dec:
        def __init__(self):
            self.buf = bytearray()
        def feed(self, data):
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
                headers_raw = frame[12:12+hl]
                payload = frame[12+hl:tl-4]
                # parse headers
                hdrs = {}
                i = 0; n = len(headers_raw)
                while i < n:
                    nl = headers_raw[i]; i += 1
                    name = headers_raw[i:i+nl].decode("utf-8","replace"); i += nl
                    vt = headers_raw[i]; i += 1
                    if vt == 7:
                        vl = struct.unpack(">H", headers_raw[i:i+2])[0]; i += 2
                        hdrs[name] = headers_raw[i:i+vl].decode("utf-8","replace"); i += vl
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
                        "--- USER MESSAGE BEGIN ---\nReply with exactly: OK"
                        "--- USER MESSAGE END ---"
                    ),
                    "userInputMessageContext": {},
                    "origin": "KIRO_CLI",
                    "modelId": model,
                }
            },
            "chatTriggerType": "MANUAL",
            "agentContinuationId": str(uuid.uuid4()),
            "agentTaskType": "vibe",
        }
    }
    headers = _headers(
        api_key, "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
    )
    req = client.build_request(
        "POST", KIRO_HOST + "/", headers=headers,
        content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    r = await client.send(req, stream=True)
    try:
        if r.status_code != 200:
            body = await r.aread()
            raise RuntimeError(
                f"probe failed: HTTP {r.status_code} {body.decode('utf-8','replace')[:300]}"
            )
        dec = _Dec()
        credits = 0.0
        async for chunk in r.aiter_bytes():
            for hdrs, pb in dec.feed(chunk):
                if hdrs.get(":event-type") == "meteringEvent" and pb:
                    data = json.loads(pb.decode("utf-8"))
                    credits += float(data.get("usage", 0.0))
        return credits
    finally:
        await r.aclose()


async def audit_key(
    client: httpx.AsyncClient, api_key: str, *, probe: bool = False
) -> QuotaReport:
    rep = QuotaReport(key_preview=_preview(api_key), ok=False)
    try:
        data = await fetch_usage_limits(client, api_key)
    except httpx.HTTPStatusError as e:
        rep.error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        return rep
    except Exception as e:
        rep.error = f"{type(e).__name__}: {e}"
        return rep

    sub = data.get("subscriptionInfo", {}) or {}
    rep.subscription_title = sub.get("subscriptionTitle")
    rep.subscription_type = sub.get("type")
    rep.overage_status = (data.get("overageConfiguration") or {}).get("overageStatus")
    rep.user_id = (data.get("userInfo") or {}).get("userId")

    # Find the Credit breakdown
    for entry in data.get("usageBreakdownList", []) or []:
        if entry.get("resourceType") == "CREDIT":
            rep.usage_limit = float(
                entry.get("usageLimitWithPrecision", entry.get("usageLimit", 0))
            )
            rep.current_usage = float(
                entry.get("currentUsageWithPrecision", entry.get("currentUsage", 0))
            )
            rep.overage_cap = float(
                entry.get("overageCapWithPrecision", entry.get("overageCap", 0))
            )
            rep.overage_rate = float(entry.get("overageRate", 0))
            ts = entry.get("nextDateReset")
            if ts:
                rep.reset_at = datetime.fromtimestamp(
                    float(ts), tz=timezone.utc
                ).isoformat()
            break

    rep.ok = True

    if probe:
        try:
            credits = await probe_one_call(client, api_key)
            rep.probe_credits_per_call = credits
            rep.probe_model = "qwen3-coder-next"
        except Exception as e:
            rep.error = (rep.error + "; " if rep.error else "") + f"probe: {e}"

    return rep


# Known approx credit cost per single short call (rate_multiplier * ~0.09)
# from meteringEvent observations. This is the unit cost of one "hi/OK"
# exchange; real-world conversations cost much more due to input context.
# Source: rateMultiplier values from ListAvailableModels.
MODEL_RATES = {
    "qwen3-coder-next": 0.05,
    "minimax-m2.1": 0.15,
    "deepseek-3.2": 0.25,
    "minimax-m2.5": 0.25,
    "claude-haiku-4.5": 0.4,
    "glm-5": 0.5,
    "auto": 1.0,
    "claude-sonnet-4.6": 1.3,
    "claude-sonnet-4.5": 1.3,
    "claude-sonnet-4": 1.3,
    "claude-opus-4.7": 2.2,
    "claude-opus-4.6": 2.2,
    "claude-opus-4.5": 2.2,
}


def estimate_calls_per_model(
    remaining: float, per_call_cost_unit: float
) -> List[tuple]:
    """per_call_cost_unit is credits-per-rate-unit (observed from probe).

    Falls back to ~0.10 credits per unit of rateMultiplier if no probe data.
    """
    out = []
    for model, rate in sorted(MODEL_RATES.items(), key=lambda p: p[1]):
        cost_per_call = rate * per_call_cost_unit
        calls = int(remaining / cost_per_call) if cost_per_call > 0 else None
        out.append((model, rate, cost_per_call, calls))
    return out


def print_report(rep: QuotaReport, *, show_estimate: bool = True) -> None:
    print(f"=== Key {rep.key_preview} ===")
    if not rep.ok:
        print(f"  FAIL: {rep.error}")
        return
    print(f"  Subscription : {rep.subscription_title} ({rep.subscription_type})")
    print(f"  User ID      : {rep.user_id}")
    print(
        f"  Usage        : {rep.current_usage:.4f} / {rep.usage_limit:.0f}  "
        f"credits  (remaining {rep.remaining:.4f})"
    )
    pct = 0.0 if not rep.usage_limit else (rep.current_usage / rep.usage_limit) * 100
    print(f"  Progress     : {pct:.2f}% used")
    print(f"  Overage      : {rep.overage_status}  (cap {rep.overage_cap:.0f}, rate ${rep.overage_rate})")
    print(f"  Resets at    : {rep.reset_at}")
    if rep.probe_credits_per_call is not None:
        # Infer per-rate-unit cost from the probe
        rate = MODEL_RATES.get(rep.probe_model, 0.05)
        per_unit = rep.probe_credits_per_call / rate if rate else 0.0
        print(
            f"  Probe        : 1 call to {rep.probe_model} cost "
            f"{rep.probe_credits_per_call:.5f} credits "
            f"(~{per_unit:.5f} credits per rate-unit)"
        )
        if show_estimate and rep.remaining is not None:
            print("  Estimated remaining short-call count per model "
                  "(based on probe sample):")
            for model, rate, cost, calls in estimate_calls_per_model(
                rep.remaining, per_unit
            ):
                print(f"    {model:<25} rate={rate:<5} ~{cost:.5f} c/call → "
                      f"{calls:>8,} calls")
    elif show_estimate and rep.remaining is not None:
        # Fallback heuristic: 1 rate-unit ≈ 0.10 credits for tiny calls
        print("  Estimated remaining short-call count per model "
              "(heuristic: ~0.10 credits per rate-unit):")
        for model, rate, cost, calls in estimate_calls_per_model(
            rep.remaining, 0.10
        ):
            print(f"    {model:<25} rate={rate:<5} ~{cost:.5f} c/call → "
                  f"{calls:>8,} calls")


def load_keys(args: argparse.Namespace) -> List[str]:
    keys: List[str] = []
    if args.keys:
        keys.extend(args.keys)
    if args.env:
        for k in (os.environ.get("KIRO_API_KEYS", "").split(",")):
            k = k.strip()
            if k:
                keys.append(k)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                k = line.strip()
                if k and not k.startswith("#"):
                    keys.append(k)
    # Deduplicate but keep order
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("keys", nargs="*", help="ksk_... keys to audit")
    p.add_argument("--env", action="store_true",
                   help="also load keys from KIRO_API_KEYS env var")
    p.add_argument("--file", help="load keys from a file, one per line")
    p.add_argument("--probe", action="store_true",
                   help="make a single cheapest-model call to sample real per-call cost")
    p.add_argument("--json", action="store_true",
                   help="output machine-readable JSON instead of tables")
    p.add_argument("--concurrency", type=int, default=5)
    return p


async def amain() -> int:
    args = build_parser().parse_args()
    keys = load_keys(args)
    if not keys:
        print("No keys provided. Use positional args, --env, or --file.",
              file=sys.stderr)
        return 2

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(args.concurrency)

        async def work(k):
            async with sem:
                return await audit_key(client, k, probe=args.probe)

        reports = await asyncio.gather(*(work(k) for k in keys))

    if args.json:
        out = [r.__dict__ | {"remaining": r.remaining} for r in reports]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for r in reports:
            print_report(r)
            print()

        # Summary
        ok = [r for r in reports if r.ok]
        bad = [r for r in reports if not r.ok]
        total_remaining = sum((r.remaining or 0.0) for r in ok)
        total_limit = sum((r.usage_limit or 0.0) for r in ok)
        print("=" * 60)
        print(f"Pool summary: {len(ok)} healthy / {len(bad)} failing / "
              f"{len(keys)} total")
        print(f"  aggregate remaining credits: {total_remaining:.4f} "
              f"/ {total_limit:.0f}")
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
