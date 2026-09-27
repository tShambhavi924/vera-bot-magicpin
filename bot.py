"""
bot.py — magicpin AI Challenge submission.

Exposes the 5 endpoints defined in challenge-testing-brief.md §2:
    POST /v1/context
    POST /v1/tick
    POST /v1/reply
    GET  /v1/healthz
    GET  /v1/metadata

Run:
    pip install fastapi uvicorn
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from composer import compose
import conversation_handlers as ch

app = FastAPI(title="vera-challenge-bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state (swap for Redis/SQLite before a real long-running deploy;
# fine for a single 60-min test window per §11 of the testing brief)
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> state dict (see conversation_handlers.py docstring)
conversations: dict[str, dict] = {}

# suppression_key -> last-sent timestamp, to avoid re-firing the same trigger
# across ticks even if the judge lists it as "active" repeatedly
_suppressed_until: dict[str, float] = {}
SUPPRESSION_TTL_SECONDS = 3 * 60 * 60  # 3h; triggers also carry their own expires_at


def _get(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "codess",
        "team_members": ["Shambhavi Tripathi"],
        "model": "rule-based-composer (no LLM call in the hot path)",
        "approach": (
            "Deterministic, template-per-trigger-kind composer over the 4 contexts. "
            "No LLM in the composition hot path (avoids hallucination + nondeterminism); "
            "an optional temperature=0 LLM polish pass can be layered on top. "
            "Multi-turn handling in conversation_handlers.py: auto-reply detection via "
            "canned-phrase + exact-repeat matching, explicit intent-transition detection, "
            "hostile/not-interested graceful exit."
        ),
        "contact_email": "tshambhavi924@gmail.com",
        "version": "0.1.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


def _resolve_for_trigger(trigger: dict) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Returns (merchant, category, customer_or_None) for a trigger, or
    (None, None, None) if required refs can't be resolved."""
    merchant_id = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
    if not merchant_id:
        return None, None, None
    merchant = _get("merchant", merchant_id)
    if not merchant:
        return None, None, None
    category = _get("category", merchant.get("category_slug", ""))
    if not category:
        return None, None, None
    customer = None
    if trigger.get("scope") == "customer":
        customer_id = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
        if customer_id:
            customer = _get("customer", customer_id)
    return merchant, category, customer


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []
    now_ts = time.time()

    try:
        sim_now = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
    except ValueError:
        sim_now = datetime.now(timezone.utc)

    for trg_id in body.available_triggers[:20]:  # respect the 20-actions-per-tick cap
        trigger = _get("trigger", trg_id)
        if not trigger:
            continue

        # skip expired triggers — compare against the judge's SIMULATED time,
        # never the wall clock (the test window is simulated, not real-time).
        expires_at = trigger.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp < sim_now:
                    continue
            except ValueError:
                pass

        skey = trigger.get("suppression_key") or trg_id
        if _suppressed_until.get(skey, 0) > now_ts:
            continue  # already sent recently for this dedup key

        merchant, category, customer = _resolve_for_trigger(trigger)
        if not merchant or not category:
            continue
        if trigger.get("scope") == "customer" and not customer:
            continue  # can't compose a customer-facing message without customer context

        result = compose(category, merchant, trigger, customer)
        if not result.get("body"):
            continue  # never emit a malformed/empty action

        conversation_id = f"conv_{merchant.get('merchant_id', 'm')}_{trg_id}_{uuid.uuid4().hex[:6]}"
        conversations[conversation_id] = {
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": customer.get("customer_id") if customer else None,
            "trigger_id": trg_id,
            "turns": [{"from": "vera", "body": result["body"]}],
            "sent_bodies": [result["body"]],
            "probed_auto_reply": False,
            "unanswered_nudges": 0,
        }
        _suppressed_until[skey] = now_ts + SUPPRESSION_TTL_SECONDS

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": customer.get("customer_id") if customer else None,
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", "")],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id,
        "customer_id": body.customer_id,
        "trigger_id": None,
        "turns": [],
        "sent_bodies": [],
        "probed_auto_reply": False,
        "unanswered_nudges": 0,
    })

    # IMPORTANT: compute the decision against history *before* this message is
    # in `turns` — ch.respond()'s auto-repeat check scans state["turns"] for
    # prior merchant/customer messages, and appending first would make the
    # message match itself on every single turn.
    decision = ch.respond(state, body.message)
    state["turns"].append({"from": body.from_role, "body": body.message})

    if decision["action"] == "send":
        out_body = decision.get("body", "")
        # anti-repetition: never resend a verbatim body in the same conversation
        if out_body in state["sent_bodies"]:
            out_body = out_body + " (following up)"
        state["turns"].append({"from": "vera", "body": out_body})
        state["sent_bodies"].append(out_body)
        return {
            "action": "send",
            "body": out_body,
            "cta": decision.get("cta", "open_ended"),
            "rationale": decision.get("rationale", ""),
        }
    elif decision["action"] == "wait":
        return {
            "action": "wait",
            "wait_seconds": decision.get("wait_seconds", 1800),
            "rationale": decision.get("rationale", ""),
        }
    else:  # end
        return {"action": "end", "rationale": decision.get("rationale", "")}
