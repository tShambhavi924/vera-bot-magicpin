# Vera Challenge Submission

## Approach

The composer (`composer.py`) is **deterministic and template-driven**, not a raw
LLM call in the hot path. For each `TriggerContext.kind`, a dedicated small
function pulls only the fields it needs out of `category`/`merchant`/`trigger`/
`customer`, fills a hand-written bilingual (English / Hindi-English-mix)
template, and returns. There is no step where the composer can invent a
number, a citation, or a competitor name — if a field isn't in the input
dicts, the template either omits that clause or falls back to a generic,
true statement.

Why not just call an LLM per the brief's "suggested approach"?

- **Hallucination is the single costliest failure mode in the rubric**
  ("hallucinated data... citing a paper not in the context" is called out
  explicitly as a penalty, and Phase 3's adaptive-injection test specifically
  rewards bots that don't invent things). A template can't drift; an LLM,
  even at temperature 0, occasionally will.
- **Determinism is a hard requirement** ("Must be deterministic given the
  same inputs"). Temperature=0 reduces but doesn't guarantee this across
  provider versions.
- **Latency/cost budget**: up to 20 actions per tick, 30s per call, `/v1/tick`
  itself has no per-action time budget breathing room if each action needs a
  network round-trip to an LLM provider. The template engine composes all 20
  in well under a second.
- Every trigger `kind` in the brief already comes with a clearly-specified
  payload shape and a clearly-specified "why now" — that is exactly the
  scenario template dispatch is built for.

The trade-off, honestly: **prose variety is lower than an LLM would produce.**
A human copywriter (or an LLM) would phrase the same facts differently every
time; my templates will sound similar across sends of the same `kind` to
different merchants. I think this is the right trade for a system that will
be judged partly on "did it hallucinate" and "was it deterministic" — but if
I had another pass, I'd add the optional LLM **polish** step described below.

### Multi-turn (`conversation_handlers.py`)

`respond()` is a priority-ordered rule cascade:

1. **Auto-reply detection** — canned-phrase match OR exact-repeat-of-a-prior-message.
   First hit: probe once ("do you want to check yourself?"). Second hit:
   `end` immediately. This mirrors the reference Pattern B in the brief
   almost exactly.
2. **Hostile / not-interested** → `end` immediately, no more pitching.
3. **Explicit intent transition** ("let's do it", "go ahead", "join karna
   hai", ...) → switches straight to an *actioning* response with no `?`,
   deliberately avoiding Pattern D (re-qualifying after a yes).
4. **Off-topic-but-not-hostile** → one honest deflection sentence, then back
   to the actual mission — doesn't pretend expertise it doesn't have.
5. **Default** → a neutral one-line acknowledgement that advances the
   conversation without introducing any new claim (since no new context was
   pushed for that turn, there's nothing new and true to say).

Anti-repetition is enforced at the transport layer (`bot.py`): if a
composed reply is identical to something already sent in that
`conversation_id`, it's suffixed rather than resent verbatim.

### Where I'd add an LLM if given more time

`composer.py` is structured so a `polish(body: str, category: Ctx) -> str`
pass could sit right after template rendering — an LLM call constrained to
*rephrasing only*, explicitly forbidden from adding any noun, number, or
name not already in the input string. That gets the variety benefit without
reopening the hallucination risk, at the cost of one more network call per
send (only really needed on the merchant-facing, higher-value sends — I
would NOT do this for every high-volume customer-facing recall message).
It's stubbed out but not wired in, gated behind `VERA_LLM_POLISH=1`, because
I'd want real eval numbers before trading determinism for it.

## What's here vs. what's a stand-in

- `bot.py`, `composer.py`, `conversation_handlers.py` — the actual submission code.
- `categories/` — the five supplied category contexts: dentists, gyms, pharmacies, restaurants, salons.
- `expanded/` — deterministic expansion generated from the supplied category files and seed data: 5 categories, 50 merchants, 200 customers, 100 triggers, plus `test_pairs.json` containing 30 canonical pairs.
- `submission_30.jsonl` — the 30 canonical outputs generated directly from `compose()` and revalidated against the same inputs.
- `dataset/` — the original local sample dataset retained for development compatibility.
- `generate_dataset.py` — deterministic dataset expansion script.
- `generate_submission.py` — development submission generator over the dataset tree.
- `smoke_test.py` — a dev-only harness. Its original paths target the older sample fixture, so the final validation used a dedicated end-to-end API check against `expanded/` instead.

## A real bug I found while building this (worth knowing)

`/v1/tick`'s trigger-expiry check was originally comparing `expires_at`
against the real wall clock instead of the judge's *simulated* `now` from
the request body. In a real 60-simulated-minute test window that starts on
some past or future date, that would have silently dropped every trigger.
Fixed in `bot.py` — compare against `body.now`, never `datetime.now()`.

## Tradeoffs

- **No LLM in the hot path** (see above) — favors determinism/no-hallucination
  over prose variety.
- **In-memory state only** — fine for the stated 60-minute single test
  window; would need Redis/SQLite for anything longer-running or multi-
  instance.
- **Template coverage is not exhaustive.** I covered every `kind` named
  explicitly in the brief's example lists (§4.3) plus a generic external-
  event fallback and a true last-resort fallback for anything else, so an
  unrecognized `kind` degrades to an honest, generic, non-hallucinating
  check-in rather than erroring or emitting an empty action.
- **Conversation-handler rules are regex/keyword-based**, not an intent
  classifier. This will miss creative phrasings of intent or hostility that
  don't match the phrase lists. An LLM-based classifier would generalize
  better; I chose keyword rules for the same determinism/latency reasons as
  the composer, and because the brief's own examples (Pattern D, the auto-
  reply hint "same message verbatim 3+ times") are themselves rule-shaped.

## What additional context would have helped most

1. **A larger real evaluation corpus** — the supplied category contexts and deterministic expansion now provide the 30 canonical pairs plus the 100-trigger development dataset; additional hidden/adaptive cases would still be useful for broader evaluation.
2. **A worked "bad" example per trigger kind** alongside the brief's "good"
   examples (Appendix A/B) — would sharpen the fallback templates for edge
   cases where a payload field is missing.
3. **Real conversation-history samples with auto-reply text in more
   languages** — my canned-phrase list is a small, hand-picked set; a real
   corpus of WhatsApp Business auto-reply strings (Hindi and English) would
   make that detector far more robust than keyword matching.

## Running locally

```bash
pip install fastapi uvicorn
uvicorn bot:app --host 0.0.0.0 --port 8080

# in another terminal, sanity-check:
python3 smoke_test.py          # starts its own server on :8099, self-contained
python3 generate_submission.py # writes submission.jsonl from dataset/
```

Deploy anywhere that gives a public URL (see testing brief §6) and submit
that URL — nothing here is deployment-specific.
