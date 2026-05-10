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



---

# kirogate_addons — pluggable components for the rotation pool

While building `kiro_quota.py` I audited the existing [kirogate
gateway](https://github.com/huasiyuuuuu/kiro/tree/kirogate-gateway) and
two high-quality reference projects
([Mirrowel/LLM-API-Key-Proxy](https://github.com/Mirrowel/LLM-API-Key-Proxy),
[VictorMinemu/CC-Router](https://github.com/VictorMinemu/CC-Router))
and extracted four drop-in components that kirogate is missing today.

Every module in `kirogate_addons/` is **single-file, stdlib + httpx
only, duck-typed against kirogate's `AccountPool`**. You can copy a file
into `kirogate/` and wire it in with ≤ 6 lines. None of them import
kirogate so they're also usable standalone.

## The table above in context: what the experiments showed

Before writing any of the cache components I ran four live probes
against the real backend (see `experiments/probe_cache*.py`). They
established:

| Test | Cold call | Warm call | Discount |
|---|---|---|---|
| 10k-token prefix, sequential | 0.01970 c | 0.01050 c | **~47%** |
| Tool-use scenario (client-provided ids) | 0.01550 c | 0.00826 c | **~47%** |
| Fresh random prefix every call | 0.01970 c | 0.01970 c | 0% |
| Explicit `cachePoint` / `cache_control` / `cacheCheckpoints` | 0.01050 c | 0.01050 c | **exactly the same as no marker** |

Two conclusions:

1. **Kiro gives you prefix caching for free.** No setup, no special
   field, no negotiation. Whenever two requests share enough leading
   tokens, the second one pays ~47% less. This is what Anthropic's
   implicit prompt cache does behind the scenes at the Bedrock layer.
2. **Any explicit cache marker in the payload is silently dropped.**
   We probed every plausible field name (`cachePoint`, `cachePoints`,
   `cache_control`, `cacheCheckpoints`, `promptCache.enabled`) at every
   placement level. Every variant costs the same to the 5th decimal.
   So there's no value in adding explicit markers — and there IS value
   in not accidentally breaking the implicit one.

`cache_stability.py` and `cache_observer.py` address those two
findings: keep the history byte-stable, and measure the resulting
savings.

## The six components

| File | Fills this gap | Integration cost |
|---|---|---|
| [`pool_strategies.py`](kirogate_addons/pool_strategies.py) | Kirogate only has blind round-robin. You can now pick **proportionally to remaining credits**, least-used, or weighted-random. Plus a per-account async concurrency semaphore so one key doesn't get slammed with 50 parallel requests and auto-throttle. | 4 lines in `pool.py` |
| [`quota_monitor.py`](kirogate_addons/quota_monitor.py) | Kirogate had zero visibility into per-key balances. This polls `GetUsageLimits` in the background, exposes a lock-free `remaining_credits(key)` lookup, and dead-key-backs-off on failure. Pair with the weighted-by-credits selector above and you finally stop wasting big reservoirs. | 5 lines in `server.py::lifespan` |
| [`account_health.py`](kirogate_addons/account_health.py) | No preflight — kirogate only discovers a dead key on the first real request. This CLI probes every key's auth, catalog access, and optionally does one minimum-cost streaming call, then classifies each key as `HEALTHY` / `LOW_CREDITS` / `THROTTLED` / `INVALID` / `NETWORK`. Exit code is non-zero on any failure so you can gate deploys on it. | Runs as CLI; `python -m kirogate_addons.account_health --file accounts.json` |
| [`hot_reload.py`](kirogate_addons/hot_reload.py) | Rotating keys required a process restart, dropping every in-flight stream. This watches the accounts file mtime and mutates the pool in place — new keys added, removed keys retired, existing keys keep their runtime state. Refuses to apply a malformed or empty file. | 3 lines in `server.py::lifespan` |
| [`cache_stability.py`](kirogate_addons/cache_stability.py) | **Kiro's backend does implicit prefix caching for free (~47% off calls after the first)** — we proved it empirically. But any randomness the translator leaks into `history` destroys byte-stability and breaks the free cache. This module deterministically rewrites known randomness sources (today: `toolUseId` random fallback) so every turn of a conversation pays the warm price. Idempotent; today usually a no-op, tomorrow defense-in-depth. | 1 line in `translator.py` |
| [`cache_observer.py`](kirogate_addons/cache_observer.py) | Kiro doesn't emit a `cache_read_input_tokens` field, so there's no way today to see whether the free prefix cache is actually hitting in production. This records `(model, prefix_sig, credits)` per call and computes hit rate + real credits saved. Exposes a `/pool/cache` endpoint's worth of data. Bounded LRU, thread-safe. | 3 lines at the streaming response path |

## Why these, not something else

Comparing kirogate to the two reference projects, these were the
biggest missing pieces:

| Concept | kirogate | Mirrowel | CC-Router | Added here |
|---|---|---|---|---|
| Round-robin rotation | ✓ | ✓ | ✓ | baseline |
| Cooldown on 429 / backoff | ✓ | ✓ | ✓ | (already good) |
| Sticky sessions | ✓ (partial) | — | — | (already good) |
| **Quota-aware routing** | — | ✓ | — | **pool_strategies + quota_monitor** |
| **Rotation modes** (balanced / sequential / random) | partial | ✓ | — | **pool_strategies** |
| **Per-account concurrency cap** | — | ✓ | — | **pool_strategies.PerAccountSemaphore** |
| **Preflight health check** | — | (implicit) | ✓ | **account_health** |
| **Hot-reload accounts file** | — | — | — | **hot_reload** |
| Credit-exhausted classification | missing | ✓ | n/a | `account_health` flags as `LOW_CREDITS` or `INVALID` |

Other things on kirogate's own ROADMAP that I **deliberately didn't
duplicate** because they already exist in kirogate:

- Prometheus `/metrics` — kirogate has it (`observability.py`).
- Token estimation — kirogate has it (`tokenizer.py`).
- `count_tokens` endpoint — already implemented.
- `contextUsageEvent` response header — already emitted.

## Quick run — end-to-end against a live key

```bash
# All addons wired together into a single smoketest. This is the test
# you should run after pulling this branch to confirm nothing's broken.
python -m kirogate_addons.integration_smoketest ksk_...
```

Expected:
```
-- start quota monitor         OK
-- start hot-reload watcher    OK
   real key remaining: 999.60
   weighted picker chose: real
   semaphore enforced peak <= 3 (observed 3)
   pool grew to 2 accounts
   selector over mixed pool hit: {'fake', 'real'}
   preflight audit status: HEALTHY  remaining=999.60
integration smoketest PASSED
```

## Integration recipe (kirogate side)

In `kirogate/pool.py`:

```python
from kirogate_addons.pool_strategies import make_selector, PerAccountSemaphore

class AccountPool:
    def __init__(self, accounts, cfg=None, *, selector=None, semaphore=None):
        ...
        self._selector  = selector  or make_selector()  # env-driven
        self._semaphore = semaphore or PerAccountSemaphore(
            limit_per_account=int(os.environ.get("KIROGATE_MAX_CONCURRENCY_PER_KEY", "4"))
        )

    def pick(self, sticky_key=None):
        # ...existing sticky + cooldown logic unchanged...
        healthy = [a for a in self.accounts if a.cooldown_until <= time.time()]
        if not healthy:
            return min(self.accounts, key=lambda a: a.cooldown_until)
        return self._selector.pick(healthy, now=time.time())
```

In `kirogate/server.py::lifespan`:

```python
from kirogate_addons.quota_monitor import QuotaMonitor
from kirogate_addons.hot_reload import AccountsFileWatcher
from kirogate_addons.pool_strategies import make_selector

async def lifespan(app):
    ...
    pool = AccountPool.from_sources(accounts_file=settings.accounts_file)
    app.state.pool = pool
    app.state.http = httpx.AsyncClient(...)

    qm = QuotaMonitor(pool=pool, http=app.state.http)
    await qm.start()
    pool._selector = make_selector(
        "weighted_credits", get_remaining=qm.remaining_credits
    )
    app.state.qm = qm

    watcher = AccountsFileWatcher(pool, path=settings.accounts_file)
    await watcher.start()
    app.state.watcher = watcher

    yield
    await watcher.stop()
    await qm.stop()
    await app.state.http.aclose()
```

Wrap upstream calls with the semaphore:

```python
async with pool._semaphore.slot(acct.key):
    async for event in generate_assistant_response_stream(...):
        ...
```

Add a new dashboard route:

```python
@app.get("/pool/quota")
async def pool_quota(...):
    _require_auth(authorization, x_api_key)
    return app.state.qm.snapshot()
```

## Environment variables added

| Var | Default | What it does |
|---|---|---|
| `KIROGATE_ROTATION_STRATEGY` | `round_robin` | `round_robin`, `least_used`, `weighted_credits`, `weighted_random` |
| `KIROGATE_ROTATION_TOLERANCE` | `0.2` | 0.0 = deterministic, 1.0 = fully random (for `weighted_random`) |
| `KIROGATE_MAX_CONCURRENCY_PER_KEY` | `4` | Max in-flight requests per single key before queueing |
| `KIROGATE_QUOTA_REFRESH_INTERVAL` | `300` | Seconds between background `GetUsageLimits` polls |

## What's still not done (you have more room to grow)

These were on kirogate's ROADMAP or showed up in the reference projects
but didn't fit a single-file drop-in so I left them out:

| **Prompt-cache checkpoints** (`cachePoints` in the translator) — needs
  editing `translator.py` directly; big win for long-history Opus calls. | **Done: cache_stability + cache_observer.** After empirical probes against the live backend, the picture was different from what `promptCaching.maximumCacheCheckpointsPerRequest` in model metadata suggests. Kiro does implicit prefix caching automatically (~47% off warm calls); explicit markers are silently dropped. The right fix is protecting byte-stability of history, not adding markers — which is what cache_stability does. |
- **envState injection** — behind a header; again lives in the translator.
- **Admin web UI** — the data it would show (`/pool/stats`,
  `/pool/quota`) is now all available; you just need a React/HTML page.
- **Docker image** — mostly a CI question, not architectural.
- **Per-client-IP rate limit at the gateway layer** — one middleware file
  away, but has policy decisions you should make.
- **Tenant-scoped `KIROGATE_KEY`** — "client A sees keys 1–3, client B
  sees keys 4–6".

Happy to drop any of those in on request.
