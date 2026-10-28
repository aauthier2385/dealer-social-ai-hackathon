"""Feature 3: salesperson handoff. When a salesperson leaves, the customer is not left behind.

For each customer, the salesperson taking over gets a short brief (what to know before the first
call, and what to talk about) and a warm introduction text in their own voice. Muse Spark writes it
from the customer's verified facts only. Code then checks every line:
  - each brief point must cite a fact it really mentions, or it is dropped;
  - nothing sensitive, nothing from another customer's voice samples;
  - the introduction never gives a reason the previous salesperson left and never speaks badly of
    anyone; it names the new salesperson and the customer, and fits in one text.
A draft that fails is replaced by a safe introduction in the same voice, and the brief says so.
"""

from __future__ import annotations

import datetime as dt
import re

from app.memory import sensitive_type
from app.meta_client import MetaBadResponse, MetaClient
from app.planner import (
    DEFAULT_VOICE,
    MAX_NOTE,
    PlanInput,
    PlanInputError,
    _text,
    _text_problem,
    custom_voice,
    mentioned_facts,
    parse_facts,
    voice_samples,
)

MAX_POINTS = 5
MAX_STARTERS = 3
MAX_POINT = 200

# Why someone left is never the customer's business, and gossip ends relationships.
_DEPARTURE = re.compile(
    r"\b(fired|quit|quitting|terminated|let go|laid off|resign\w*|no longer (?:with|here|at)|"
    r"(?:isn'?t|is not) (?:with us|here) anymore|left (?:the|our) (?:company|store|dealership)|"
    r"parted ways|moved on|unfortunately|sorry to say|bad news|complain\w*)\b",
    re.IGNORECASE,
)


def _point_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "point": {"type": "string"},
            "uses_facts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["point", "uses_facts"],
        "additionalProperties": False,
    }


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "know_first": {"type": "array", "items": _point_schema()},
        "talk_about": {"type": "array", "items": _point_schema()},
        "intro_text": {"type": "string"},
        "intro_call_notes": {"type": "array", "items": {"type": "string"}},
        "intro_uses_facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "headline",
        "know_first",
        "talk_about",
        "intro_text",
        "intro_call_notes",
        "intro_uses_facts",
    ],
    "additionalProperties": False,
}

HANDOFF_HEAD = """You help a car dealership keep a customer when their salesperson leaves. The \
salesperson taking over has never met this customer. From the customer's verified relationship \
facts (with ids), write a handoff brief and a warm first introduction from the new salesperson.

The new salesperson's voice:
"""

HANDOFF_RULES = """

Write:
- headline: one sentence on who this customer is, from the facts (vehicle, family, what matters).
- know_first: 3 to 5 short points the new salesperson should know before the first call. Each \
cites the fact ids it uses.
- talk_about: 2 or 3 conversation starters, phrased as what to ask. Each cites its fact ids.
- intro_text: the new salesperson's first text to the customer, under 300 characters. Greet the \
customer by first name, introduce yourself as "<new salesperson first name> at <store>", say you \
will be taking care of them from here on, mention ONE personal detail from the facts, and offer \
help. Warm, no pressure, no sales pitch.
- intro_call_notes: 2 to 4 short bullets for a first phone call instead of a text.
- intro_uses_facts: the fact ids the intro text uses.

Rules:
- Use ONLY the facts given. Never invent details. Facts with the same "about" are the same \
individual (a pet's name and breed are one animal).
- A fact with certainty "uncertain" may only be asked about or marked "may", never stated as true.
- Never say or hint why the previous salesperson left, and never say anything negative about them, \
the store or the customer. You may say they asked you to look after the customer only as "I'll be \
taking care of you from here on".
- Never mention money, prices, payments, credit, health, a death, legal matters or other sensitive \
topics.
"""


def handoff_system(plan: PlanInput) -> str:
    voice = custom_voice(plan.voice_samples) if plan.voice_samples else DEFAULT_VOICE
    return HANDOFF_HEAD + voice + HANDOFF_RULES


def parse_input(data: dict) -> tuple[PlanInput, str, str]:
    """The customer as a PlanInput (with the NEW salesperson), the customer-since date, and the
    salesperson who is leaving."""
    from_sp = _text(data, "from_salesperson", 60)
    to_sp = _text(data, "to_salesperson", 60)
    if from_sp.lower() == to_sp.lower():
        raise PlanInputError("'from_salesperson' and 'to_salesperson' must be different people.")
    raw_since = _text(data, "customer_since", 10, required=False)
    since = dt.date(2000, 1, 1)
    if raw_since:
        try:
            since = dt.date.fromisoformat(raw_since)
        except ValueError as exc:
            raise PlanInputError("'customer_since' must look like 2026-10-27.") from exc
    samples, sample_names = voice_samples(data)
    plan = PlanInput(
        customer_name=_text(data, "customer_name", 80),
        vehicle=_text(data, "vehicle", 80, required=False) or "vehicle",
        salesperson_name=to_sp,
        store_name=_text(data, "store_name", 60, required=False) or "Demo Motors",
        delivery_date=since,
        birthday=None,
        facts=parse_facts(data),
        voice_samples=samples,
        sample_names=sample_names,
    )
    return plan, (raw_since and since.isoformat()) or "", from_sp


def _fallback_intro(plan: PlanInput) -> str:
    return (
        f"Hey {plan.first_name}, it's {plan.salesperson_first} at {plan.store_name}. I'll be "
        f"taking care of you from here on out. Hope the {plan.vehicle} is treating you right, "
        "and let me know if you ever need anything!"
    )


def intro_problem(text: str, plan: PlanInput) -> str:
    """Why an introduction text is unusable, or ''."""
    problem = _text_problem(text, "handoff", plan)
    if problem:
        return problem
    if _DEPARTURE.search(text):
        return "departure_reason"
    lower = text.lower()
    if plan.salesperson_first.lower() not in lower:
        return "missing_new_salesperson"
    if plan.first_name.lower() not in lower:
        return "missing_customer_name"
    return ""


def _ids(raw, plan: PlanInput) -> list[dict]:
    known = {f.id: f for f in plan.facts}
    refs, seen = [], set()
    for fid in raw if isinstance(raw, list) else []:
        if isinstance(fid, str) and fid in known and fid not in seen:
            seen.add(fid)
            refs.append({"id": fid, "fact": known[fid].fact})
    return refs


def _points(raw, plan: PlanInput, limit: int, checks: dict) -> list[dict]:
    """Keep only points that are safe and really mention a fact they cite."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        point = " ".join(str(item.get("point", "")).split())[:MAX_POINT]
        refs = mentioned_facts(_ids(item.get("uses_facts"), plan), point, plan)
        if not point or sensitive_type(point) or _DEPARTURE.search(point) or not refs:
            checks["points_dropped"] += 1
            continue
        uncertain = any(
            f.certainty == "uncertain" for f in plan.facts if f.id in {r["id"] for r in refs}
        )
        out.append({"point": point, "uses_facts": refs, "not_sure": uncertain})
        if len(out) >= limit:
            break
    return out


async def brief(meta: MetaClient, plan: PlanInput, since: str, from_sp: str) -> dict:
    fact_lines = "\n".join(
        f"{f.id} [{f.category}] ({f.certainty}{', ' + f.timing if f.timing else ''}) "
        f"about {f.about or plan.customer_name}: {f.fact}"
        for f in plan.facts
    )
    user = (
        f"Customer: {plan.customer_name}\nVehicle: {plan.vehicle}\n"
        f"Customer since: {since or 'unknown'}\nPrevious salesperson: {from_sp}\n"
        f"New salesperson: {plan.salesperson_name}\nStore: {plan.store_name}\n\n"
        f"Verified facts:\n{fact_lines or '(none)'}"
    )
    data, result = await meta.chat_json(
        [{"role": "system", "content": handoff_system(plan)}, {"role": "user", "content": user}],
        BRIEF_SCHEMA,
        "handoff_brief",
        reasoning_effort="low",
    )
    if not isinstance(data.get("know_first"), list):
        raise MetaBadResponse("brief missing know_first")

    checks = {"points_kept": 0, "points_dropped": 0, "intro_replaced_with_safe_draft": False}
    know_first = _points(data.get("know_first"), plan, MAX_POINTS, checks)
    talk_about = _points(data.get("talk_about"), plan, MAX_STARTERS, checks)
    checks["points_kept"] = len(know_first) + len(talk_about)

    headline = " ".join(str(data.get("headline", "")).split())[:240]
    if not headline or sensitive_type(headline) or _DEPARTURE.search(headline):
        headline = f"{plan.customer_name} drives a {plan.vehicle}."

    intro = " ".join(str(data.get("intro_text", "")).split())
    problem = intro_problem(intro, plan)
    if problem:
        checks["intro_replaced_with_safe_draft"] = True
        intro = _fallback_intro(plan)
    intro_refs = mentioned_facts(_ids(data.get("intro_uses_facts"), plan), intro, plan)

    notes = []
    for note in data.get("intro_call_notes", []) or []:
        if not isinstance(note, str):
            continue
        note = " ".join(note.split())[:MAX_NOTE]
        if note and not sensitive_type(note) and not _DEPARTURE.search(note):
            notes.append(note)
    if not notes:
        notes = [
            f"Introduce yourself as {plan.salesperson_first} at {plan.store_name}",
            f"Ask how the {plan.vehicle} is doing",
            "Let them know you're their go-to from here on",
        ]

    return {
        "customer": plan.customer_name,
        "from_salesperson": from_sp,
        "to_salesperson": plan.salesperson_name,
        "headline": headline,
        "know_first": know_first,
        "talk_about": talk_about,
        "intro": {
            "text": intro,
            "call_notes": notes[:4],
            "uses_facts": intro_refs,
            "safe_draft_used": [f"text:{problem}"] if problem else [],
        },
        "checks": checks,
        "voice": {
            "source": "salesperson_samples" if plan.voice_samples else "store_default",
            "samples_used": len(plan.voice_samples),
        },
        "usage": result.usage,
        "latency_ms": result.latency_ms,
        "model": result.model,
    }
