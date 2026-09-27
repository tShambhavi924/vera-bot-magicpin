"""
conversation_handlers.py — respond() for in-flight conversations.

Implements the "open challenges" from challenge-brief.md §12:
  1. Auto-reply detection (same canned text repeated, or canned-phrase match)
  2. Intent-transition handling (explicit "yes/let's do it" -> action mode)
  3. Graceful exit (hostile / not-interested / 3 unanswered nudges)
  4. Off-topic deflection without abandoning the mission

`state` is a plain dict the caller (bot.py) owns and persists per
conversation_id. Shape:

    {
        "merchant_id": str,
        "customer_id": str | None,
        "trigger_id": str,
        "turns": [{"from": "vera"|"merchant"|"customer", "body": str}, ...],
        "sent_bodies": [str, ...],       # anti-repetition check
        "probed_auto_reply": bool,       # have we already tried once?
        "unanswered_nudges": int,
    }
"""

from __future__ import annotations

import re
from typing import Any

AUTO_REPLY_PHRASES = [
    "thank you for contacting",
    "we will get back to you",
    "hamari team tak pahuncha",
    "automated assistant",
    "we have received your message",
    "will revert shortly",
    "shukriya, hum jald hi",
]

INTENT_PHRASES = [
    "let's do it", "lets do it", "go ahead", "yes please", "ok let's do it",
    "chalo karte hain", "haan chalo", "start karo", "shuru karo",
    "i want to join", "join karna hai", "sign me up", "yes go ahead",
    "proceed", "confirm karo", "haan confirm",
]

QUALIFYING_MARKERS = ["would you", "do you", "can you tell", "what if", "how about", "kya aap"]

HOSTILE_PHRASES = [
    "stop messaging", "spam", "useless", "unsubscribe", "harass",
    "band karo", "pareshan mat karo", "fraud",
]

NOT_INTERESTED_PHRASES = [
    "not interested", "no thanks", "nahi chahiye", "abhi nahi",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _matches_any(text: str, phrases: list[str]) -> bool:
    t = _norm(text)
    return any(p in t for p in phrases)


def _is_exact_repeat(state: dict, message: str) -> bool:
    incoming = [t["body"] for t in state.get("turns", []) if t.get("from") in ("merchant", "customer")]
    return sum(1 for m in incoming if _norm(m) == _norm(message)) >= 1  # prior occurrence(s) exist


def respond(state: dict, merchant_message: str) -> dict:
    """Given the conversation-so-far (in `state`) and the latest inbound
    message, decide the next move. Returns a dict shaped like the
    /v1/reply response contract: {action, body?, wait_seconds?, rationale}.
    """
    state.setdefault("turns", [])
    state.setdefault("sent_bodies", [])
    state.setdefault("probed_auto_reply", False)
    state.setdefault("unanswered_nudges", 0)

    is_canned = _matches_any(merchant_message, AUTO_REPLY_PHRASES)
    is_repeat = _is_exact_repeat(state, merchant_message)

    # --- 1. Auto-reply handling -------------------------------------------------
    if is_canned or is_repeat:
        if state["probed_auto_reply"]:
            return {
                "action": "end",
                "rationale": "Second canned/repeated reply confirms this is an auto-responder, not the owner. Exiting to avoid burning turns (brief §12.1, Pattern B).",
            }
        state["probed_auto_reply"] = True
        body = "Samajh gayi. Team tak pahunchane se pehle, kya aap khud 2 minute mein dekhna chahenge exactly kya missing hai? Chalega?"
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_stop",
            "rationale": "First canned-reply detection — one probe to confirm human-vs-auto before disengaging.",
        }

    # --- 2. Hostile / not-interested --------------------------------------------
    if _matches_any(merchant_message, HOSTILE_PHRASES):
        return {
            "action": "end",
            "rationale": "Hostile message detected — apologizing and disengaging immediately rather than continuing to pitch.",
        }
    if _matches_any(merchant_message, NOT_INTERESTED_PHRASES):
        return {
            "action": "end",
            "rationale": "Explicit not-interested signal — graceful exit per brief §12.5.",
        }

    # --- 3. Intent transition ----------------------------------------------------
    if _matches_any(merchant_message, INTENT_PHRASES):
        body = "Great — starting now. Done shortly and I will confirm here once it is live."
        return {
            "action": "send",
            "body": body,
            "cta": "none",
            "rationale": "Explicit affirmative intent detected — switching straight to action mode, not another qualifying question (brief §12.2, avoiding Pattern D).",
        }

    # --- 4. Off-topic but not hostile: stay on mission, answer briefly ----------
    off_topic_markers = ["gst", "tax filing", "loan", "insurance"]
    if any(m in merchant_message.lower() for m in off_topic_markers):
        body = "That's outside what I handle directly, so I won't be much help there — happy to keep going on the profile/marketing side whenever you're ready."
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Off-topic request acknowledged without pretending expertise; redirected back to the conversation's actual purpose.",
        }

    # --- 5. Default: acknowledge + advance ---------------------------------------
    turn_count = len([t for t in state["turns"] if t.get("from") in ("merchant", "customer")])
    if turn_count >= 3 and state["unanswered_nudges"] >= 3:
        return {
            "action": "end",
            "rationale": "3+ nudges without forward progress — exiting gracefully rather than continuing to push (brief §12.5).",
        }

    body = "Got it — noted. Want me to go ahead with that, or is there something specific you'd like checked first?"
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "Neutral acknowledgement advancing the conversation one step; no new claim introduced since no new context was pushed for this turn.",
    }
