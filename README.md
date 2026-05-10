# kiro-quota

A zero-side-effect quota probe for Kiro CLI 2.0 headless API keys (`ksk_...`).

Given one or more `ksk_` keys (e.g. the keys in your rotation pool),
`kiro_quota.py` prints how many credits each key has left, its
subscription tier, when it resets, and an estimate of how many requests
you can still make on each model before exhausting it.

## Why this exists

The Kiro backend does not return a "credits remaining" field on normal
`GenerateAssistantResponse` calls — it only emits a `meteringEvent` with
the cost of the current call. So if you run a rotation pool of `ksk_`
keys (see
[`huasiyuuuuu/kiro`](https://github.com/huasiyuuuuu/kiro/tree/feat/kiro-account-pool)),
there is no obvious way to answer "how many more calls can this key
make?" without burning credits.

This tool uses the undocumented AWS CodeWhisperer operation
`AmazonCodeWhispererService.GetUsageLimits`, which returns:

- `subscriptionInfo.subscriptionTitle` (e.g. `KIRO PRO`)
- `subscriptionInfo.type` (e.g. `Q_DEVELOPER_STANDALONE_PRO`)
- `usageBreakdownList[].usageLimitWithPrecision` — total credits
- `usageBreakdownList[].currentUsageWithPrecision` — credits already spent
- `usageBreakdownList[].nextDateReset` — reset timestamp (UNIX seconds)
- `usageBreakdownList[].overageCap`, `overageRate`, and whether overage
  billing is enabled
- `overageConfiguration.overageStatus` (`ENABLED` / `DISABLED`)
- `userInfo.userId`

Calling it does **not** consume any credits — it's the same path the CLI
uses to render its own quota indicator.

## Installation

```bash
pip install httpx
```

That's it. Single-file script, Python 3.8+.

## Usage

```bash
# Single key, no live call — zero cost.
python kiro_quota.py ksk_XXXXXXXXXXXXXX

# Multiple keys
python kiro_quota.py ksk_AAA ksk_BBB ksk_CCC

# From env var (one-api / kirogate style)
export KIRO_API_KEYS=ksk_AAA,ksk_BBB,ksk_CCC
python kiro_quota.py --env

# From a file, one key per line
python kiro_quota.py --file keys.txt

# Calibrate per-call cost with a single cheapest-model probe call
# (costs ~0.005 credits per key).
python kiro_quota.py --probe --env

# Machine-readable output
python kiro_quota.py --json --env
```

## Example output

```
=== Key ksk_ZnRLp7…P7xd ===
  Subscription : KIRO PRO (Q_DEVELOPER_STANDALONE_PRO)
  User ID      : d-9067642ac7.546874e8-1041-703a-6816-0a95b8044cc3
  Usage        : 0.1900 / 1000  credits  (remaining 999.8100)
  Progress     : 0.02% used
  Overage      : DISABLED  (cap 10000, rate $0.04)
  Resets at    : 2026-06-01T00:00:00+00:00
  Probe        : 1 call to qwen3-coder-next cost 0.00506 credits (~0.10120 credits per rate-unit)
  Estimated remaining short-call count per model (based on probe sample):
    qwen3-coder-next          rate=0.05  ~0.00506 c/call →  197,589 calls
    claude-haiku-4.5          rate=0.4   ~0.04048 c/call →   24,698 calls
    claude-sonnet-4.6         rate=1.3   ~0.13156 c/call →    7,599 calls
    claude-opus-4.7           rate=2.2   ~0.22264 c/call →    4,490 calls
    ...
============================================================
Pool summary: 1 healthy / 0 failing / 1 total
  aggregate remaining credits: 999.8100 / 1000
```

## How to interpret "estimated calls"

The estimate assumes a **short, context-free** call (roughly what the
probe measures: a 1-word reply, no system prompt, no tools, no history).

Real calls cost more for two reasons:

1. **Input tokens**: every message in the conversation history is
   re-sent on every turn. A 20-message chat on Opus can easily cost
   0.8–2 credits per reply.
2. **Tool use and images**: each adds input volume. Vision requests
   typically cost 2–5× a text-only request on the same model.

So treat the numbers as an **upper bound** for ping-sized calls. Halve
them for realistic chat-length turns, and divide by ~10 for long
conversations with tools and files attached.

## What the endpoint looks like

```
POST https://q.us-east-1.amazonaws.com/
Authorization:   Bearer ksk_...
Content-Type:    application/x-amz-json-1.0
tokentype:       API_KEY                            <-- required for ksk_ keys
X-Amz-Target:    AmazonCodeWhispererService.GetUsageLimits
{}
```

Response is plain JSON (not eventstream), so no decoder is required.

## Safety

- `GetUsageLimits` is idempotent and free.
- `--probe` spends `~0.005` credits per key (1 call to the cheapest
  model). On a 1000-credit Pro plan that's 0.0005% of the monthly
  quota. Skip it if you want truly zero impact.
- Keys are redacted in log output (first 10 + last 4).
- No keys are persisted anywhere by this script.

## Pair it with a rotation pool

If you already run a gateway like
[`kirogate`](https://github.com/huasiyuuuuu/kiro/tree/kirogate-gateway),
point it at the same file or env var:

```bash
export KIRO_API_KEYS=ksk_aaa,ksk_bbb,ksk_ccc
python kiro_quota.py --env            # quota dashboard
python -m kirogate                    # gateway using the same keys
```

The gateway's `/pool/stats` tells you which keys are in cooldown and
which have failed; this tool tells you which ones are running low. Use
both together to decide which keys to retire / top-up.
