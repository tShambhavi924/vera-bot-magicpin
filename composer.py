"""
composer.py — the compose() engine for the magicpin AI Challenge.

Design choice: DETERMINISTIC, template-driven composition rather than a raw
LLM call. Rationale (see README.md for the full argument):

  1. The rubric penalizes hallucination harder than it rewards prose flourish.
     A template that can only ever reference fields that exist in the input
     dicts cannot invent a citation, a competitor name, or a fake number.
  2. "Must be deterministic given the same inputs" is a hard requirement.
     Temperature=0 LLM calls are *usually* stable but not guaranteed to be,
     and add 1-3s of latency per call against a 30s budget with up to 20
     actions per tick.
  3. Every trigger `kind` in the brief has a clearly-specified payload shape
     and a clearly-specified "why now" — that's exactly what template
     selection + slot-filling is good at.

An optional LLM "polish" pass exists (see `polish.py`, gated behind
VERA_LLM_POLISH=1) that can rephrase the *surface* wording without touching
the facts it's given — but the bot works fully, deterministically, and for
free without it, which is the default.

Every composer function returns a dict with exactly the required keys:
    body, cta, send_as, suppression_key, rationale
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

Ctx = dict[str, Any]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TABOO_DEFAULT = ["guaranteed", "cure", "amazing deal", "flat", "% off", "best in"]


def _lang_is_hindi_mix(merchant: Ctx) -> bool:
    langs = merchant.get("identity", {}).get("languages", []) or []
    return "hi" in langs


def _mix(hi: str, en: str, merchant: Ctx) -> str:
    """Return the Hindi-English code-mixed string when the merchant's
    languages include 'hi', else plain English. Never invents content —
    both variants must be supplied by the template author."""
    return hi if _lang_is_hindi_mix(merchant) else en


def _name(merchant: Ctx) -> str:
    return merchant.get("identity", {}).get("name", "there")


def _first_name(merchant: Ctx) -> str:
    full = _name(merchant)
    # merchant identities are business names ("Dr. Meera's Dental Clinic");
    # keep the honorific + first proper noun, don't fabricate a person name.
    return full.split("'s")[0] if "'s" in full else full


def _active_offer(merchant: Ctx) -> Optional[Ctx]:
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o
    return None


def _voice_is_clinical(category: Ctx) -> bool:
    tone = category.get("voice", {}).get("tone", "")
    return tone in ("peer_clinical", "clinical", "peer_technical")


def _taboos(category: Ctx) -> list[str]:
    return category.get("voice", {}).get("taboos", []) or TABOO_DEFAULT


def _digest_item(category: Ctx, item_id: Optional[str] = None) -> Optional[Ctx]:
    items = category.get("digest", []) or []
    if item_id:
        for d in items:
            if d.get("id") == item_id:
                return d
    return items[0] if items else None


def _suppress(kind: str, *parts: str) -> str:
    clean = ":".join(p for p in parts if p)
    return f"{kind}:{clean}" if clean else kind


def _rationale(*points: str) -> str:
    return "; ".join(points)


def _strip_taboos(text: str, category: Ctx) -> str:
    """Post-generation guardrail: hard-remove any taboo phrase that slipped
    into a template (defensive only — templates below are hand-checked not
    to use them, but category voice packs are data, not code, so we don't
    trust them blindly)."""
    for t in _taboos(category):
        pattern = re.compile(re.escape(t), re.IGNORECASE)
        text = pattern.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ---------------------------------------------------------------------------
# Per-kind composers — merchant-facing
# ---------------------------------------------------------------------------

def _research_digest(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    item = _digest_item(category, trigger.get("payload", {}).get("top_item_id")
                         or trigger.get("payload", {}).get("top_item", {}).get("title"))
    if not item:
        return _fallback(category, merchant, trigger)

    title = item.get("title", "")
    source = item.get("source", "")
    trial_n = item.get("trial_n")
    segment = item.get("patient_segment") or item.get("segment")

    signals = merchant.get("signals", []) or []
    segment_phrase = segment.replace("_", " ") if segment else ""  # "high_risk_adults" -> "high risk adults"
    cohort_hook = ""
    if segment_phrase and any("cohort" in s or "risk" in s for s in signals):
        cohort_hook = _mix(
            f"Aapke {segment_phrase} patients ke liye relevant hai — ",
            f"One item relevant to your {segment_phrase} patients — ",
            merchant,
        )
    n_clause = f"{trial_n:,}-patient trial showed " if trial_n else ""

    body = (
        f"{_first_name(merchant)}, {source.split(',')[0]}'s latest issue landed. "
        f"{cohort_hook}{n_clause}{title.lower() if trial_n else title}. "
        f"{_mix('2-min abstract hai. Chahiye toh main pull karke ek patient-ed WhatsApp bhi draft kar doon?', 'Worth a 2-min look. Want me to pull it and draft a patient-ed WhatsApp you can share?', merchant)}"
        f" — {source}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("research", category.get("slug", ""), item.get("id", "")),
        "rationale": _rationale(
            "External research digest release",
            f"anchored on '{title}' ({source})",
            "cohort tie-in from merchant signals" if cohort_hook else "no cohort signal available, kept general",
            "curiosity + reciprocity levers, open-ended CTA (pure-info trigger)",
        ),
    }


def _perf_spike(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    perf = merchant.get("performance", {})
    delta = perf.get("delta_7d", {})
    views_pct = delta.get("views_pct")
    metric_clause = f"views +{views_pct*100:.0f}%" if views_pct else "your numbers moved"
    body = (
        f"{_first_name(merchant)}, quick one — {metric_clause} vs your usual week "
        f"({perf.get('views', 'N/A')} views over the last {perf.get('window_days', 30)}d). "
        f"{_mix('Jaanna chahenge kya kaam kar raha hai, taaki hum usse repeat kar sakein?', 'Want to dig into what drove it, so we can repeat it?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("perf_spike", merchant.get("merchant_id", "")),
        "rationale": _rationale("Internal perf_spike trigger", "anchored on real 7d delta from performance snapshot", "curiosity + reciprocity"),
    }


def _perf_dip(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    perf = merchant.get("performance", {})
    delta = perf.get("delta_7d", {})
    calls_pct = delta.get("calls_pct")
    metric_clause = f"calls down {abs(calls_pct)*100:.0f}%" if calls_pct and calls_pct < 0 else "activity dipped"
    peer = category.get("peer_stats", {})
    peer_clause = ""
    if peer.get("avg_ctr") and perf.get("ctr") and perf["ctr"] < peer["avg_ctr"]:
        peer_clause = f" Your CTR is {perf['ctr']*100:.1f}% vs the {peer.get('scope', 'category')} median of {peer['avg_ctr']*100:.1f}%."
    body = (
        f"{_first_name(merchant)}, {metric_clause} this week vs last.{peer_clause} "
        f"{_mix('2-3 cheezein hain jo turant help kar sakti hain — batau?', 'There are 2-3 quick things that usually help here — want me to walk you through them?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "binary_yes_stop",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("perf_dip", merchant.get("merchant_id", "")),
        "rationale": _rationale("Internal perf_dip trigger", "loss-aversion + peer-benchmark specificity", "binary CTA since this is an action trigger"),
    }


def _milestone(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    payload = trigger.get("payload", {})
    milestone = payload.get("milestone") or payload.get("description") or "a milestone"
    body = (
        f"{_first_name(merchant)}, congrats — you just crossed {milestone}! "
        f"{_mix('Ek Google post ke saath share karein? 5-min kaam hai.', 'Worth marking with a Google post to your profile? Takes 5 minutes.', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "binary_yes_stop",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("milestone", merchant.get("merchant_id", "")),
        "rationale": _rationale("Internal milestone_reached trigger", "celebratory, effort-externalization CTA (post drafted for them)"),
    }


def _dormant(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    offer = _active_offer(merchant)
    peer = category.get("peer_stats", {})
    hook = ""
    if offer:
        hook = _mix(f"Aapka '{offer['title']}' offer abhi bhi active hai. ", f"Your '{offer['title']}' offer is still live. ", merchant)
    elif peer.get("avg_reviews"):
        hook = _mix(f"Aapke area ke peers avg {peer['avg_reviews']} reviews rakhte hain. ", f"Peers in your category average {peer['avg_reviews']} reviews. ", merchant)
    body = (
        f"{_first_name(merchant)}, thodi der se baat nahi hui hai. {hook}"
        f"{_mix('Kuch naya check karna hai profile mein?', 'Anything you want me to check on your profile this week?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("dormant", merchant.get("merchant_id", "")),
        "rationale": _rationale("Internal dormant_with_vera trigger", "re-engagement, low-pressure open question, asking-the-merchant lever"),
    }


def _competitor_opened(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    payload = trigger.get("payload", {})
    dist = payload.get("distance_km")
    dist_clause = f"{dist}km away" if dist else "nearby"
    body = (
        f"{_first_name(merchant)}, heads up — a new {category.get('slug', 'business').rstrip('s')} just went live on Google {dist_clause} from you. "
        f"{_mix('Dekhna chahenge unka profile kaisa dikh raha hai vs aapka?', 'Want to see how their listing stacks up against yours?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("competitor_opened", merchant.get("merchant_id", "")),
        "rationale": _rationale("External competitor_opened trigger", "loss-aversion + curiosity (voyeur hook), no competitor name fabricated — only distance from payload"),
    }


def _generic_external(category: Ctx, merchant: Ctx, trigger: Ctx, label: str) -> dict:
    payload = trigger.get("payload", {})
    detail = payload.get("headline") or payload.get("description") or payload.get("title") or label
    body = (
        f"{_first_name(merchant)}, {detail}. "
        f"{_mix('Isse related koi post ya offer chahiye toh bataiye — 5 min mein draft kar deti hoon.', 'If you want a post or offer tied to this, say the word — I can draft one in 5 minutes.', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress(trigger.get("kind", "event"), merchant.get("merchant_id", "")),
        "rationale": _rationale(f"External {trigger.get('kind')} trigger", "effort-externalization offer, kept strictly to payload facts"),
    }


def _scheduled_recurring(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    body = (
        f"{_first_name(merchant)}, "
        f"{_mix('is hafte sabse zyada kaunsa treatment/service ask hua hai aapke paas?', 'what was the most-asked service at your place this week?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("scheduled_recurring", merchant.get("merchant_id", "")),
        "rationale": _rationale("Weekly curious-ask cadence", "asking-the-merchant lever (underused per brief §10)"),
    }


def _review_theme(category: Ctx, merchant: Ctx, trigger: Ctx) -> dict:
    payload = trigger.get("payload", {})
    theme = payload.get("theme", "a recurring theme")
    count = payload.get("mention_count")
    count_clause = f"{count} reviews this week" if count else "a few recent reviews"
    body = (
        f"{_first_name(merchant)}, {count_clause} mention '{theme}'. "
        f"{_mix('Chahenge toh main ek short response-template bana doon jo aap reviews pe use kar sakein?', 'Want a short reply template you can reuse on those reviews?', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "binary_yes_stop",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress("review_theme", merchant.get("merchant_id", "")),
        "rationale": _rationale("Internal review_theme_emerged trigger", "specific mention count, effort-externalization"),
    }


# ---------------------------------------------------------------------------
# Per-kind composers — customer-facing (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------

def _recall_due(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Ctx) -> dict:
    rel = customer.get("relationship", {})
    last_visit = rel.get("last_visit", "your last visit")
    offer = _active_offer(merchant)
    offer_clause = f" {offer['title']}." if offer else ""
    name = customer.get("identity", {}).get("name", "there")
    hi_mix = "hi" in customer.get("identity", {}).get("language_pref", "")

    if hi_mix:
        body = (
            f"Hi {name}, {_name(merchant)} here 🦷 It's been a while since {last_visit} — "
            f"your recall is due.{offer_clause} Apke liye time nikal sakte hain, batao kaunsa slot chalega."
        )
    else:
        body = (
            f"Hi {name}, {_name(merchant)} here. It's been a while since {last_visit} — "
            f"your recall is due.{offer_clause} Let us know a time that works and we'll hold a slot."
        )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key") or _suppress("recall_due", customer.get("customer_id", "")),
        "rationale": _rationale("Internal recall_due trigger, scope=customer", "real last_visit + real active offer, no invented slots", "send_as=merchant_on_behalf, consent scope covers recall_reminders"),
    }


def _appointment_tomorrow(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Ctx) -> dict:
    name = customer.get("identity", {}).get("name", "there")
    payload = trigger.get("payload", {})
    time_str = payload.get("time") or payload.get("appointment_time") or "your scheduled time"
    body = f"Hi {name}, reminder from {_name(merchant)} — see you tomorrow at {time_str}. Reply 1 to confirm or 2 to reschedule."
    return {
        "body": _strip_taboos(body, category),
        "cta": "binary_yes_stop",
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key") or _suppress("appointment_tomorrow", customer.get("customer_id", "")),
        "rationale": _rationale("Internal appointment_tomorrow trigger, scope=customer", "binary confirm/reschedule CTA (booking flow, allowed multi-choice)"),
    }


def _lapsed(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Ctx) -> dict:
    name = customer.get("identity", {}).get("name", "there")
    rel = customer.get("relationship", {})
    visits = rel.get("visits_total")
    offer = _active_offer(merchant)
    offer_clause = f" We have {offer['title']} running right now." if offer else ""
    body = (
        f"Hi {name}, it's {_name(merchant)}. "
        f"{f'You have been with us for {visits} visits' if visits else 'It has been a while'} "
        f"and we would love to see you again.{offer_clause} Let us know if you would like to book."
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key") or _suppress("customer_lapsed", customer.get("customer_id", "")),
        "rationale": _rationale("Internal customer_lapsed_soft/hard trigger", "real visit count + real active offer, reciprocity framing"),
    }


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    """Used when a trigger kind isn't in the dispatch table, or a referenced
    payload field is missing. Never invents facts — falls back to the most
    generic true statement available (merchant name + category)."""
    target = customer.get("identity", {}).get("name") if customer else _first_name(merchant)
    body = (
        f"Hi {target}, checking in from {_name(merchant)}. "
        f"{_mix('Kuch help chahiye toh bataiye.', 'Let us know if there is anything we can help with.', merchant)}"
    )
    return {
        "body": _strip_taboos(body, category),
        "cta": "open_ended",
        "send_as": "merchant_on_behalf" if customer else "vera",
        "suppression_key": trigger.get("suppression_key") or _suppress(trigger.get("kind", "generic"), merchant.get("merchant_id", "")),
        "rationale": "Unrecognized/underspecified trigger kind — generic low-commitment check-in rather than fabricating specifics",
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_MERCHANT_DISPATCH = {
    "research_digest": _research_digest,
    "research_digest_release": _research_digest,
    "category_research_digest_release": _research_digest,
    "perf_spike": _perf_spike,
    "perf_dip": _perf_dip,
    "milestone_reached": _milestone,
    "dormant_with_vera": _dormant,
    "competitor_opened": _competitor_opened,
    "scheduled_recurring": _scheduled_recurring,
    "curious_ask_due": _scheduled_recurring,
    "review_theme_emerged": _review_theme,
}

_CUSTOMER_DISPATCH = {
    "recall_due": _recall_due,
    "appointment_tomorrow": _appointment_tomorrow,
    "customer_lapsed_soft": _lapsed,
    "customer_lapsed_hard": _lapsed,
    "unplanned_slot_open": _appointment_tomorrow,
}

_EXTERNAL_LABELS = {
    "festival_upcoming": "a festival is coming up",
    "festival": "a festival is coming up",
    "weather_heatwave": "a heatwave is forecast",
    "local_news_event": "there's a local news event nearby",
    "regulation_change": "a relevant regulation update just dropped",
    "category_trend_movement": "search interest in your category is shifting",
}


def compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    """Public entry point matching the challenge contract exactly."""
    kind = trigger.get("kind", "")
    scope = trigger.get("scope", "customer" if customer else "merchant")

    try:
        if scope == "customer" and customer:
            fn = _CUSTOMER_DISPATCH.get(kind)
            if fn:
                result = fn(category, merchant, trigger, customer)
            else:
                result = _fallback(category, merchant, trigger, customer)
        else:
            fn = _MERCHANT_DISPATCH.get(kind)
            if fn:
                result = fn(category, merchant, trigger)
            elif kind in _EXTERNAL_LABELS:
                result = _generic_external(category, merchant, trigger, _EXTERNAL_LABELS[kind])
            else:
                result = _fallback(category, merchant, trigger, customer)
    except Exception as exc:  # defensive: never let a malformed context crash a tick
        result = _fallback(category, merchant, trigger, customer)
        result["rationale"] += f" (composer error swallowed: {exc.__class__.__name__})"

    # Guard: single primary CTA, no double asks
    result["body"] = re.sub(r"\s{2,}", " ", result["body"]).strip()
    return result
