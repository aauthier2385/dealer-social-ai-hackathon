"""Feature 2: verified relationship facts -> a long-range follow-up plan in the salesperson's voice.

The dates come from the dealership's own cadence and are calculated in code, never by the model:
  day after the sale (call or text), thank-you card within 48 hours, 30-day call, first service at
  90 days, birthday (only if known) and the purchase anniversary.
Muse Spark writes the content for each touchpoint and suggests up to three extra check-ins from life
events it was told about. Code then checks every draft: it may only use facts it was given, must
not touch anything sensitive, must fit in a text message, birthday texts must be pure celebration,
and the service text must carry the booking link. A draft that fails a check is replaced by a safe
draft in the same voice, and the plan says so.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from app.memory import sensitive_type
from app.meta_client import MetaBadResponse, MetaClient

MAX_TEXT = 320
MAX_NOTE = 160
MAX_CARD = 600
MAX_FACTS = 60
MAX_LIFE_EVENTS = 3
MAX_SAMPLES = 5
MIN_SAMPLE = 10
BOOKING = "{booking_link}"

SLOT_TITLES = {
    "day1": "Day-after check-in",
    "card": "Thank-you card (mail within 48 hours)",
    "day30": "30-day call",
    "day90": "First service visit (90 days)",
    "birthday": "Birthday",
    "anniversary": "Purchase anniversary",
}

_BUSINESS = re.compile(
    r"\b(service|deal|offer|trade|appointment|price|payment|sale|special|discount|financ\w*|"
    r"lease|upgrade|inventory|rebate|promo\w*)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\{[^}]*\}")
# "Hey John," / "Good morning Kerrigan!" -> the greeted customer's name, swapped out of voice samples.
_GREETING = re.compile(
    r"^(\s*(?:hey|hi|hello|yo|morning|good (?:morning|afternoon|evening)|what'?s up|happy birthday)"
    r"[,!]?\s+)([A-Z][a-zA-Z'’-]+)\b",
    re.IGNORECASE,
)
_NOT_NAMES = {"there", "everyone", "all", "guys", "man", "buddy", "friend", "sir", "maam", "ma'am"}


class PlanInputError(ValueError):
    """Bad planner input. The message is safe to show."""


@dataclass
class Fact:
    id: str
    about: str
    category: str
    fact: str
    timing: str
    certainty: str


@dataclass
class PlanInput:
    customer_name: str
    vehicle: str
    salesperson_name: str
    store_name: str
    delivery_date: dt.date
    birthday: tuple[int, int] | None
    facts: list[Fact]
    voice_samples: list[str] = None  # the salesperson's own texts, customer names swapped out
    sample_names: frozenset = (
        frozenset()
    )  # names greeted in those samples; drafts must not use them

    def __post_init__(self):
        if self.voice_samples is None:
            self.voice_samples = []

    @property
    def first_name(self) -> str:
        return self.customer_name.split()[0]

    @property
    def salesperson_first(self) -> str:
        return self.salesperson_name.split()[0]


def _text(data: dict, key: str, limit: int, required: bool = True, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise PlanInputError(f"'{key}' must be text.")
    value = " ".join(value.split())[:limit]
    if required and not value:
        raise PlanInputError(f"'{key}' is required.")
    return value


def anonymize_sample(text: str) -> tuple[str, str]:
    """Swap the greeted customer's name in a voice sample for {first_name}. Returns (text, name)."""
    match = _GREETING.match(text)
    if not match or match[2].lower() in _NOT_NAMES:
        return text, ""
    name = match[2]
    text = re.sub(rf"\b{re.escape(name)}\b", "{first_name}", text)
    return text, name


def _voice_samples(data: dict) -> tuple[list[str], frozenset]:
    raw = data.get("voice_samples", [])
    if raw is None:
        return [], frozenset()
    if not isinstance(raw, list) or len(raw) > MAX_SAMPLES:
        raise PlanInputError(f"'voice_samples' must be a list of up to {MAX_SAMPLES} texts.")
    samples, names = [], set()
    for item in raw:
        if not isinstance(item, str):
            raise PlanInputError("Each voice sample must be text.")
        text = " ".join(item.split())
        if len(text) < MIN_SAMPLE or len(text) > MAX_TEXT:
            continue  # too short to show a style, or not a text message
        if sensitive_type(text):
            continue  # a sample about money or health must not steer the drafts
        text, name = anonymize_sample(text)
        if name:
            names.add(name.lower())
        samples.append(text)
    return samples, frozenset(names)


def parse_input(data: dict) -> PlanInput:
    try:
        delivery = dt.date.fromisoformat(_text(data, "delivery_date", 10))
    except ValueError as exc:
        raise PlanInputError("'delivery_date' must look like 2026-10-27.") from exc
    if not 2000 <= delivery.year <= 2100:
        raise PlanInputError("'delivery_date' is out of range.")

    birthday = None
    raw_birthday = _text(data, "birthday", 5, required=False)
    if raw_birthday:
        match = re.fullmatch(r"(\d{2})-(\d{2})", raw_birthday)
        if not match:
            raise PlanInputError("'birthday' must look like 03-14 (month-day), or be left empty.")
        month, day = int(match[1]), int(match[2])
        try:
            dt.date(2024, month, day)  # a leap year accepts 02-29
        except ValueError as exc:
            raise PlanInputError("'birthday' is not a real date.") from exc
        birthday = (month, day)

    raw_facts = data.get("facts", [])
    if not isinstance(raw_facts, list) or len(raw_facts) > MAX_FACTS:
        raise PlanInputError(f"'facts' must be a list of up to {MAX_FACTS} facts.")
    facts: list[Fact] = []
    for n, raw in enumerate(raw_facts, start=1):
        if not isinstance(raw, dict):
            raise PlanInputError("Each fact must be an object.")
        f = Fact(
            id=_text(raw, "id", 8, required=False) or f"F{n}",
            about=_text(raw, "about", 80, required=False),
            category=_text(raw, "category", 30, required=False) or "personal",
            fact=_text(raw, "fact", 300),
            timing=_text(raw, "timing", 60, required=False),
            certainty="uncertain" if raw.get("certainty") == "uncertain" else "stated",
        )
        # Defense in depth: the memory step already withholds these, but callers may send anything.
        if not sensitive_type(f"{f.fact} {f.timing}"):
            facts.append(f)

    samples, sample_names = _voice_samples(data)
    return PlanInput(
        customer_name=_text(data, "customer_name", 80),
        vehicle=_text(data, "vehicle", 80, required=False) or "vehicle",
        salesperson_name=_text(data, "salesperson_name", 60),
        store_name=_text(data, "store_name", 60, required=False) or "Demo Motors",
        delivery_date=delivery,
        birthday=birthday,
        facts=facts,
        voice_samples=samples,
        sample_names=sample_names,
    )


def _add_years(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return day.replace(year=day.year + years, day=28)


def contact_channel(facts: list[Fact]) -> str:
    """'text', 'call' or 'call_or_text', from what the customer said about being contacted."""
    likes_text = likes_call = False
    for f in facts:
        if f.category != "contact_preference":
            continue
        said = f.fact.lower()
        texts, calls = "text" in said, "call" in said
        no_calls = any(x in said for x in ("don't call", "do not call", "not call", "no calls"))
        prefers = "prefer" in said or "rather" in said
        if texts and (no_calls or (prefers and said.find("text") < said.find("call"))):
            likes_text = True  # "prefers texts over calls", "text him, don't call"
            continue
        if calls and prefers and said.find("call") < said.find("text"):
            likes_call = True  # "prefers calls over texts"
            continue
        likes_text = likes_text or texts
        likes_call = likes_call or (calls and not no_calls)
    if likes_text and not likes_call:
        return "text"
    if likes_call and not likes_text:
        return "call"
    return "call_or_text"


def schedule(plan: PlanInput) -> list[dict]:
    """The dealership cadence, computed in code."""
    d = plan.delivery_date
    slots = [
        {"slot": "day1", "date": d + dt.timedelta(days=1), "channel": contact_channel(plan.facts)},
        {"slot": "card", "date": d + dt.timedelta(days=2), "channel": "mail"},
        {"slot": "day30", "date": d + dt.timedelta(days=30), "channel": "call"},
        {"slot": "day90", "date": d + dt.timedelta(days=90), "channel": "text"},
    ]
    if plan.birthday:
        month, day = plan.birthday
        year = d.year
        while True:
            try:
                candidate = dt.date(year, month, day)
            except ValueError:  # Feb 29 in a non-leap year
                candidate = dt.date(year, 2, 28)
            if candidate > d:
                break
            year += 1
        slots.append({"slot": "birthday", "date": candidate, "channel": "call"})
    slots.append({"slot": "anniversary", "date": _add_years(d, 1), "channel": "call"})
    return slots


def _strs(kind="string", **extra):
    return {"type": kind, **extra}


def plan_schema(slot_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slot": _strs(enum=slot_names),
                        "text": _strs(),
                        "call_notes": {"type": "array", "items": _strs()},
                        "card": _strs(),
                        "uses_facts": {"type": "array", "items": _strs()},
                        "why": _strs(),
                    },
                    "required": ["slot", "text", "call_notes", "card", "uses_facts", "why"],
                    "additionalProperties": False,
                },
            },
            "life_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact_id": _strs(),
                        "date": _strs(),
                        "title": _strs(),
                        "channel": _strs(enum=["text", "call"]),
                        "text": _strs(),
                        "call_notes": {"type": "array", "items": _strs()},
                        "why": _strs(),
                    },
                    "required": [
                        "fact_id",
                        "date",
                        "title",
                        "channel",
                        "text",
                        "call_notes",
                        "why",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["slots", "life_events"],
        "additionalProperties": False,
    }


DEFAULT_VOICE = """The salesperson's real texts, to copy tone, length and style:
- Check-in: "Hey John, it's Andrew at Demo Motors. Just checking in to see how you're doing and if I \
can help you with anything about your new Ford Bronco. Let me know if you need anything!"
- Birthday: "Hey Kerrigan, I just wanted to wish you a happy birthday 🎂 🥳 🎉 🎈!! Enjoy your day and \
I hope it's a great one!"
- Anniversary: "Hey Gary, just wanted to check in to see how you're doing and how your Mustang is \
holding up, I can't believe it's been a year!! Holler at me if you need anything!"
Style: first name, short and warm, no pressure, exclamation points are fine, correct spelling."""

PLAN_HEAD = """You write follow-ups for a car salesperson who keeps customers for life. You are \
given the customer's verified relationship facts (with ids) and a schedule of touchpoints whose \
dates are already fixed. For every slot, write the content in the salesperson's own voice.

"""

PLAN_RULES = """

Slots:
- day1 (channel text, call or call_or_text): check-in the day after the sale, introducing yourself \
as "<salesperson first name> at <store>". Fill "text" for text, "call_notes" for call, both for \
call_or_text.
- card: "card" = a 2-4 sentence handwritten thank-you note mailed with the delivery photo. Mention \
the vehicle and one personal detail. Sign with the salesperson's first name.
- day30: "call_notes" = 3-5 short bullets: how the vehicle is going, plus personal things to ask \
about, taken only from the facts.
- day90: "text" inviting them in for their first service visit. Include the literal text \
{booking_link} where the booking link goes.
- birthday: "text" = pure celebration, emojis welcome, absolutely nothing about the vehicle, service, \
deals or business. "call_notes" = 1-2 light bullets.
- anniversary: "text" in the salesperson's anniversary style (or their general style), naming the vehicle and that it has been a \
year; "call_notes" = 2-3 bullets.
Leave fields a slot does not use as "" or [].

life_events: up to 3 extra check-ins for facts that have a timing and certainty "stated" (a child \
turning 16, a lease ending, a yearly trip). Give a sensible date (YYYY-MM-DD) after the delivery \
date and within two years of it, a short title, a channel, and the text or call notes. Be caring, \
not salesy: you may offer help, never push.

Rules:
- Use ONLY the facts given, and list the ids you used in uses_facts. Never invent details.
- Facts with the same "about" describe the same individual. A pet's name and its breed are one \
animal ("Bill, your little Frenchie"), never two.
- A fact with certainty "uncertain" may only be asked about, never stated.
- Never mention money, prices, payments, credit, health, a death, legal matters or other sensitive \
topics.
- Texts must be under 300 characters.
- why: one short sentence explaining the personal touch."""

PLAN_SYSTEM = PLAN_HEAD + DEFAULT_VOICE + PLAN_RULES


def custom_voice(samples: list[str]) -> str:
    lines = "\n".join(f"- {s}" for s in samples)
    return (
        "The salesperson's own real texts (the customer's name is shown as {first_name}). Copy "
        "their voice exactly: greeting, how they introduce themselves, sign-off, length, "
        "punctuation, capitalization, slang and emoji use. Keep their style even for birthdays "
        "and anniversaries.\n"
        f"{lines}\n"
        "Use these ONLY for style. Never reuse a name, vehicle or any detail from them; every "
        "detail must come from the verified facts. Spelling should still be correct."
    )


def plan_system(plan: PlanInput) -> str:
    voice = custom_voice(plan.voice_samples) if plan.voice_samples else DEFAULT_VOICE
    return PLAN_HEAD + voice + PLAN_RULES


def _fallbacks(plan: PlanInput) -> dict[str, dict]:
    first, sp, store, vehicle = (
        plan.first_name,
        plan.salesperson_first,
        plan.store_name,
        plan.vehicle,
    )
    hello = f"Hey {first}, it's {sp} at {store}."
    return {
        "day1": {
            "text": f"{hello} Just checking in to see how you're doing and if I can help you with "
            f"anything about your new {vehicle}. Let me know if you need anything!",
            "call_notes": [f"Ask how the new {vehicle} is treating them", "Answer any questions"],
        },
        "card": {
            "card": f"{first}, thank you for choosing {store}! Enjoy your new {vehicle}, and let me "
            f"know if you ever need anything. - {sp}"
        },
        "day30": {
            "call_notes": [
                f"Ask how the {vehicle} has been for the first month",
                "Check whether any features need a walkthrough",
            ]
        },
        "day90": {
            "text": f"{hello} Your {vehicle} is coming up on its first service visit. Pick a time "
            f"that works for you here: {BOOKING} Let me know if you need anything!"
        },
        "birthday": {
            "text": f"Hey {first}, I just wanted to wish you a happy birthday 🎂 🥳 🎉!! Enjoy your "
            "day and I hope it's a great one!",
            "call_notes": ["Wish them a happy birthday, nothing else"],
        },
        "anniversary": {
            "text": f"Hey {first}, just wanted to check in to see how you're doing and how your "
            f"{vehicle} is holding up. I can't believe it's been a year!! Holler at me if you "
            "need anything!",
            "call_notes": [f"Ask how the {vehicle} has been after a year"],
        },
    }


def _borrowed_name(text: str, plan: PlanInput) -> bool:
    """True if a draft greets or mentions a customer from the voice samples instead of this one."""
    if not plan.sample_names:
        return False
    own = {plan.first_name.lower(), plan.salesperson_first.lower()}
    words = {w.lower() for w in re.findall(r"[A-Za-z'’-]+", text)}
    return bool((plan.sample_names - own) & words)


def _text_problem(text: str, slot: str, plan: PlanInput | None = None) -> str:
    """Why a draft text is unusable, or ''."""
    if not text.strip():
        return "empty"
    if len(text) > MAX_TEXT:
        return "too_long"
    if sensitive_type(text):
        return "sensitive"
    others = [p for p in _PLACEHOLDER.findall(text) if p != BOOKING]
    if others or (BOOKING in text and slot != "day90"):
        return "placeholder"
    if slot == "birthday" and _BUSINESS.search(text):
        return "business_on_birthday"
    if plan is not None and _borrowed_name(text, plan):
        return "name_from_voice_sample"
    return ""


def _clean_notes(notes, slot: str) -> list[str]:
    out = []
    for note in notes if isinstance(notes, list) else []:
        if not isinstance(note, str):
            continue
        note = " ".join(note.split())[:MAX_NOTE]
        if not note or sensitive_type(note) or _PLACEHOLDER.search(note):
            continue
        if slot == "birthday" and _BUSINESS.search(note):
            continue
        out.append(note)
    return out[:6]


async def plan_touchpoints(meta: MetaClient, plan: PlanInput) -> dict:
    slots = schedule(plan)
    facts_by_id = {f.id: f for f in plan.facts}
    fact_lines = "\n".join(
        f"{f.id} [{f.category}] ({f.certainty}{', ' + f.timing if f.timing else ''}) "
        f"about {f.about or plan.customer_name}: {f.fact}"
        for f in plan.facts
    )
    slot_lines = "\n".join(
        f"- {s['slot']}: {s['date'].isoformat()} via {s['channel']}" for s in slots
    )
    user = (
        f"Customer: {plan.customer_name}\nVehicle: {plan.vehicle}\n"
        f"Salesperson: {plan.salesperson_name}\nStore: {plan.store_name}\n"
        f"Delivery date: {plan.delivery_date.isoformat()}\n\n"
        f"Verified facts:\n{fact_lines or '(none)'}\n\nSchedule:\n{slot_lines}"
    )
    data, result = await meta.chat_json(
        [{"role": "system", "content": plan_system(plan)}, {"role": "user", "content": user}],
        plan_schema([s["slot"] for s in slots]),
        "touchpoint_plan",
        reasoning_effort="low",
    )
    if not isinstance(data.get("slots"), list):
        raise MetaBadResponse("plan missing slots")

    drafts: dict[str, dict] = {}
    for raw in data["slots"]:
        if isinstance(raw, dict) and isinstance(raw.get("slot"), str):
            drafts.setdefault(raw["slot"], raw)

    fallbacks = _fallbacks(plan)
    checks = {
        "drafted": 0,
        "replaced_with_safe_draft": 0,
        "life_events_kept": 0,
        "life_events_dropped": 0,
    }
    touchpoints: list[dict] = []

    def fact_refs(ids) -> list[dict]:
        refs, seen = [], set()
        for fid in ids if isinstance(ids, list) else []:
            if isinstance(fid, str) and fid in facts_by_id and fid not in seen:
                seen.add(fid)
                refs.append({"id": fid, "fact": facts_by_id[fid].fact})
        return refs

    for s in slots:
        slot, channel = s["slot"], s["channel"]
        raw = drafts.get(slot, {})
        fallback = fallbacks[slot]
        problems: list[str] = []
        tp = {
            "date": s["date"].isoformat(),
            "kind": slot,
            "title": SLOT_TITLES[slot],
            "channel": channel,
            "text": "",
            "call_notes": [],
            "card": "",
            "uses_facts": fact_refs(raw.get("uses_facts")),
            "why": "",
            "safe_draft_used": [],
        }
        why = str(raw.get("why", "")).strip()[:200]
        tp["why"] = "" if sensitive_type(why) else why

        wants_text = channel in ("text", "call_or_text") or slot in ("birthday", "anniversary")
        wants_notes = channel in ("call", "call_or_text")
        if wants_text:
            text = " ".join(str(raw.get("text", "")).split())
            if (
                slot == "day90"
                and text
                and BOOKING not in text
                and not _text_problem(text, slot, plan)
            ):
                text = f"{text} {BOOKING}"
            problem = _text_problem(text, slot, plan)
            if problem:
                problems.append(f"text:{problem}")
                text = fallback["text"]
            tp["text"] = text
        if wants_notes:
            notes = _clean_notes(raw.get("call_notes"), slot)
            if not notes:
                problems.append("call_notes:empty")
                notes = fallback.get("call_notes", [])
            tp["call_notes"] = notes
        if slot == "card":
            card = " ".join(str(raw.get("card", "")).split())
            if (
                not card
                or len(card) > MAX_CARD
                or sensitive_type(card)
                or _borrowed_name(card, plan)
                or _PLACEHOLDER.search(card)
            ):
                problems.append("card:" + ("empty" if not card else "failed_check"))
                card = fallback["card"]
            tp["card"] = card

        tp["safe_draft_used"] = problems
        if problems:
            checks["replaced_with_safe_draft"] += 1
            wanted = int(wants_text) + int(wants_notes) + int(slot == "card")
            if len(problems) >= wanted:  # nothing the model wrote survived
                tp["uses_facts"] = []
        else:
            checks["drafted"] += 1
        touchpoints.append(tp)

    latest = _add_years(plan.delivery_date, 2)
    for raw in data.get("life_events", []) if isinstance(data.get("life_events"), list) else []:
        if checks["life_events_kept"] >= MAX_LIFE_EVENTS:
            break
        if not isinstance(raw, dict):
            continue
        fact = facts_by_id.get(raw.get("fact_id"))
        try:
            when = dt.date.fromisoformat(str(raw.get("date", "")))
        except ValueError:
            when = None
        channel = raw.get("channel") if raw.get("channel") in ("text", "call") else "call"
        text = " ".join(str(raw.get("text", "")).split())
        notes = _clean_notes(raw.get("call_notes"), "life_event")
        title = " ".join(str(raw.get("title", "")).split())[:80]
        usable = (
            fact is not None
            and fact.timing
            and fact.certainty == "stated"
            and when is not None
            and plan.delivery_date < when <= latest
            and title
            and not sensitive_type(title)
            and (
                (channel == "text" and not _text_problem(text, "life_event", plan))
                or (channel == "call" and notes)
            )
        )
        if not usable:
            checks["life_events_dropped"] += 1
            continue
        checks["life_events_kept"] += 1
        why = str(raw.get("why", "")).strip()[:200]
        touchpoints.append(
            {
                "date": when.isoformat(),
                "kind": "life_event",
                "title": title,
                "channel": channel,
                "text": text if channel == "text" else "",
                "call_notes": notes if channel == "call" else [],
                "card": "",
                "uses_facts": [{"id": fact.id, "fact": fact.fact}],
                "why": "" if sensitive_type(why) else why,
                "safe_draft_used": [],
            }
        )

    touchpoints.sort(key=lambda t: t["date"])
    for n, tp in enumerate(touchpoints, start=1):
        tp["id"] = f"T{n}"
    return {
        "customer": plan.customer_name,
        "touchpoints": touchpoints,
        "checks": checks,
        "voice": {
            "source": "salesperson_samples" if plan.voice_samples else "store_default",
            "samples_used": len(plan.voice_samples),
        },
        "usage": result.usage,
        "latency_ms": result.latency_ms,
        "model": result.model,
    }
