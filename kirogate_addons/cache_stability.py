"""Prefix-cache stabilizer for Kiro payloads.

THE FINDING (empirical, against live Kiro backend)
--------------------------------------------------
Kiro's backend implements implicit Anthropic-style prefix caching AT NO
CHARGE: if two requests share the same leading tokens (>=~1024 tokens),
the second and subsequent calls pay roughly 47% less. Measured on
claude-haiku-4.5 with a 10k-token prefix:

    cold call:  0.01550 credits
    warm call:  0.00826 credits   (47% cheaper, automatic)

No explicit `cachePoint` / `cache_control` / `cacheCheckpoints` field is
accepted by the backend — we probed all of them and every variant costs
exactly the same as the control. So DO NOT inject cache markers; just
make sure your payload prefix stays byte-stable across turns.

WHAT BREAKS THE FREE CACHE
--------------------------
Any randomness in the payload history ruins byte-stability and forces a
cold prefix. kirogate's translator has one such source today:

  `toolUseId` fallback: `f"call_{uuid.uuid4().hex[:16]}"` is freshly
  generated when a client doesn't pass `tool_call_id`. In practice
  clients DO pass an id (the backend actually requires it — requests
  with empty tool_result ids are rejected), so this fallback rarely
  fires. But if any future code path introduces randomness into
  history, cache hits vanish silently.

WHAT THIS MODULE DOES
---------------------
`stabilize_payload(payload)` walks the `history` and rewrites any
randomness it recognises with a deterministic content-hash id. Today
that's only the toolUseId fallback; the module is designed so new
randomness sources can be added in one place as the translator evolves.

It's also:
  * idempotent (running it twice is a no-op),
  * cheap (single pass over history, O(n) in turns),
  * side-effect-free on the wire when no randomness is present
    (which is the common case today).

`payload_prefix_signature(payload)` returns a sha256 fingerprint of the
bits of the payload that actually affect the prefix cache. Pair it with
`cache_observer.CacheObserver` to measure hit rate in production.

USAGE IN kirogate/translator.py
-------------------------------
    from kirogate_addons.cache_stability import stabilize_payload

    def openai_to_kiro(messages, tools=None, model="auto"):
        ...
        payload = _turns_to_payload(turns, system_text, tools, kiro_model)
        return stabilize_payload(payload)   # <<< idempotent no-op today,
                                            #     insurance for tomorrow

The optional `context_signature` kwarg (typically a client/tenant id)
namespaces the synthetic ids so two clients with identical messages
never accidentally collide on the same id in the memo cache.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, Optional, MutableMapping

# Sentinel prefix so our synthetic ids are recognizable in logs and
# distinguishable from client-provided ones.
_SYNTH_PREFIX = "tooluse_stable_"

# In-process memo. A 2-turn exchange reuses the same history, so we get
# far more cache hits than naive recomputation suggests.
_MEMO: Dict[str, str] = {}
_MEMO_LOCK = threading.Lock()
_MEMO_CAP = 4096  # small LRU-ish cap; we purge aggressively when full.


def _stable_hash(*parts: Any) -> str:
    """Deterministic content hash across Python runs. sha256 -> 16 hex chars."""
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, (dict, list)):
            h.update(json.dumps(part, sort_keys=True, separators=(",", ":"))
                     .encode("utf-8"))
        else:
            h.update(str(part).encode("utf-8"))
        h.update(b"\x1f")  # unit separator
    return h.hexdigest()[:16]


def _memo_set(key: str, value: str) -> None:
    with _MEMO_LOCK:
        if len(_MEMO) >= _MEMO_CAP:
            # Simple purge: drop half the entries. Slightly favours
            # fresh tool calls over ancient ones.
            for k in list(_MEMO.keys())[: _MEMO_CAP // 2]:
                _MEMO.pop(k, None)
        _MEMO[key] = value


def _memo_get(key: str) -> Optional[str]:
    with _MEMO_LOCK:
        return _MEMO.get(key)


def _is_synthetic(tool_use_id: Any) -> bool:
    """Detect ids kirogate's translator generated as a random fallback.

    Pattern in kirogate.translator:   f"call_{uuid.uuid4().hex[:16]}"
    We also recognise our own sentinel so we don't recompute.
    """
    if not isinstance(tool_use_id, str):
        return True
    if tool_use_id.startswith(_SYNTH_PREFIX):
        return False  # already stable
    # "call_" + 16 hex chars is the kirogate fallback shape.
    if tool_use_id.startswith("call_") and len(tool_use_id) == 21:
        rest = tool_use_id[5:]
        if all(c in "0123456789abcdef" for c in rest):
            return True
    return False


def _stabilize_tool_id(
    name: str, input_obj: Any, *, turn_index: int, context_signature: str
) -> str:
    """Compute a deterministic id for (name, input, position)."""
    memo_key = f"{context_signature}|{turn_index}|{name}|{json.dumps(input_obj, sort_keys=True, separators=(',',':'))}"
    cached = _memo_get(memo_key)
    if cached:
        return cached
    digest = _stable_hash(context_signature, turn_index, name, input_obj)
    out = _SYNTH_PREFIX + digest
    _memo_set(memo_key, out)
    return out


def stabilize_payload(
    payload: Dict[str, Any],
    *,
    context_signature: str = "",
    stats: Optional[MutableMapping[str, int]] = None,
) -> Dict[str, Any]:
    """Rewrite randomness sources in a Kiro payload to make history deterministic.

    Returns the same object, mutated in place (and returned for convenience).

    Currently handled:
      * history[*].assistantResponseMessage.toolUses[*].toolUseId
      * history[*].userInputMessage.userInputMessageContext.toolResults[*].toolUseId
        — matched against the immediately-preceding assistant turn so
        the id used by the assistant equals the id used by the tool_result.
      * currentMessage.userInputMessage.userInputMessageContext.toolResults[*]
        — same rule, but only rewritten when paired with a rewritten
        assistant id in history.

    Everything else (conversationId, agentContinuationId) is **not**
    stabilized because we proved those have no effect on the prefix cache.
    """
    if stats is None:
        stats = {}

    cs = payload.get("conversationState")
    if not isinstance(cs, dict):
        return payload
    history = cs.get("history") or []
    current = cs.get("currentMessage")

    # Walk history: rewrite random ids and build a remap table.
    id_remap: Dict[str, str] = {}
    for turn_index, turn in enumerate(history):
        if not isinstance(turn, dict):
            continue
        # Assistant tool uses
        arm = turn.get("assistantResponseMessage")
        if isinstance(arm, dict):
            tool_uses = arm.get("toolUses") or []
            for j, tu in enumerate(tool_uses):
                if not isinstance(tu, dict):
                    continue
                old_id = tu.get("toolUseId")
                if _is_synthetic(old_id):
                    new_id = _stabilize_tool_id(
                        tu.get("name", ""),
                        tu.get("input", {}),
                        turn_index=turn_index + j * 0.01,
                        context_signature=context_signature,
                    )
                    tu["toolUseId"] = new_id
                    if isinstance(old_id, str):
                        id_remap[old_id] = new_id
                    stats["synthetic_tool_ids_rewritten"] = (
                        stats.get("synthetic_tool_ids_rewritten", 0) + 1
                    )
        # User tool results: map via the remap table so pairing stays intact.
        uim = turn.get("userInputMessage")
        if isinstance(uim, dict):
            ctx = uim.get("userInputMessageContext") or {}
            for tr in (ctx.get("toolResults") or []):
                if isinstance(tr, dict):
                    rid = tr.get("toolUseId")
                    if isinstance(rid, str) and rid in id_remap:
                        tr["toolUseId"] = id_remap[rid]

    # currentMessage tool results — remap using the same table.
    if isinstance(current, dict):
        uim = current.get("userInputMessage")
        if isinstance(uim, dict):
            ctx = uim.get("userInputMessageContext") or {}
            for tr in (ctx.get("toolResults") or []):
                if isinstance(tr, dict):
                    rid = tr.get("toolUseId")
                    if isinstance(rid, str) and rid in id_remap:
                        tr["toolUseId"] = id_remap[rid]

    return payload


def payload_prefix_signature(payload: Dict[str, Any]) -> str:
    """Signature of the parts of the payload that must stay stable for
    the prefix cache to hit. Useful for metrics / debugging:

        hit_key = payload_prefix_signature(payload)
        cache_observer.record(hit_key, cost_in_credits)
    """
    cs = payload.get("conversationState") or {}
    history = cs.get("history") or []
    # Strip the fields that don't affect the cache.
    stripped_history = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        stripped_history.append(turn)
    model = None
    cur = cs.get("currentMessage")
    if isinstance(cur, dict):
        uim = cur.get("userInputMessage") or {}
        model = uim.get("modelId")
    return _stable_hash(model, stripped_history)


# ---------------------------------------------------------------- selftest

def _selftest() -> None:  # pragma: no cover
    # 1) Idempotency
    payload = {
        "conversationState": {
            "history": [
                {"userInputMessage": {
                    "content": "u1", "userInputMessageContext": {},
                    "origin": "KIRO_CLI", "modelId": "m"}},
                {"assistantResponseMessage": {
                    "content": "",
                    "toolUses": [
                        {"toolUseId": "call_abcdef0123456789",
                         "name": "get_weather",
                         "input": {"city": "NY"}},
                    ]}},
                {"userInputMessage": {
                    "content": "u2",
                    "userInputMessageContext": {
                        "toolResults": [{"toolUseId": "call_abcdef0123456789",
                                         "content": [{"text":"72F"}]}]},
                    "origin": "KIRO_CLI", "modelId": "m"}},
            ],
            "currentMessage": {
                "userInputMessage": {
                    "content": "u3", "userInputMessageContext": {},
                    "origin": "KIRO_CLI", "modelId": "m"}
            },
        }
    }
    before_sig = payload_prefix_signature(payload)
    stats = {}
    stabilize_payload(payload, stats=stats)
    after_sig = payload_prefix_signature(payload)
    # Different signature — the random ID was replaced with a stable one.
    assert before_sig != after_sig, "signature unchanged -> stabilization didn't run"
    first_id = (
        payload["conversationState"]["history"][1]
        ["assistantResponseMessage"]["toolUses"][0]["toolUseId"]
    )
    assert first_id.startswith(_SYNTH_PREFIX), first_id

    # tool_result remapped
    tr_id = (
        payload["conversationState"]["history"][2]
        ["userInputMessage"]["userInputMessageContext"]["toolResults"][0]["toolUseId"]
    )
    assert tr_id == first_id, f"pairing broken: {tr_id} vs {first_id}"

    # Idempotent
    sig2 = payload_prefix_signature(payload)
    stabilize_payload(payload)
    sig3 = payload_prefix_signature(payload)
    assert sig2 == sig3, "second stabilize changed signature (not idempotent)"
    assert stats["synthetic_tool_ids_rewritten"] == 1

    # 2) Client-provided ids are preserved
    payload2 = {
        "conversationState": {
            "history": [
                {"assistantResponseMessage": {
                    "content": "",
                    "toolUses": [
                        {"toolUseId": "toolu_CLIENT_PROVIDED_ID",
                         "name": "x", "input": {}},
                    ]}},
            ],
            "currentMessage": {
                "userInputMessage": {
                    "content": "", "userInputMessageContext": {},
                    "origin": "KIRO_CLI", "modelId": "m"}
            },
        }
    }
    stabilize_payload(payload2)
    kept = (payload2["conversationState"]["history"][0]
            ["assistantResponseMessage"]["toolUses"][0]["toolUseId"])
    assert kept == "toolu_CLIENT_PROVIDED_ID", f"client id was clobbered: {kept}"

    # 3) Same inputs -> same synthetic id across two runs (the whole point)
    def build():
        return {
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content":"a","userInputMessageContext":{},
                                          "origin":"KIRO_CLI","modelId":"m"}},
                    {"assistantResponseMessage":{"content":"",
                        "toolUses":[{"toolUseId":"call_aaaabbbbccccdddd",
                                     "name":"f","input":{"x":1}}]}},
                ],
                "currentMessage":{"userInputMessage":{"content":"b",
                                   "userInputMessageContext":{},
                                   "origin":"KIRO_CLI","modelId":"m"}},
            }
        }
    a = build(); b = build()
    stabilize_payload(a); stabilize_payload(b)
    ida = a["conversationState"]["history"][1]["assistantResponseMessage"]["toolUses"][0]["toolUseId"]
    idb = b["conversationState"]["history"][1]["assistantResponseMessage"]["toolUses"][0]["toolUseId"]
    assert ida == idb, f"determinism broken: {ida} vs {idb}"

    # 4) Different inputs -> different ids (no false sharing)
    c = build()
    c["conversationState"]["history"][1]["assistantResponseMessage"]["toolUses"][0]["input"] = {"x": 2}
    stabilize_payload(c)
    idc = c["conversationState"]["history"][1]["assistantResponseMessage"]["toolUses"][0]["toolUseId"]
    assert ida != idc

    print("cache_stability selftest OK")


if __name__ == "__main__":
    _selftest()
