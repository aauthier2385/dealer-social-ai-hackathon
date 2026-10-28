"""Feature 6: relationships at risk. A weekly brief that tells a manager which customers need
attention, why, and what to do next.

Finding the customers is plain code, so it costs nothing however many customers a store has:
overdue follow-ups, a salesperson who has left, long silence, and life events coming up soon are
scored and ranked. Only the top few are sent to Muse Spark, which explains each one and suggests a
next step. Code then checks every explanation: it must cite the signals it is based on, may only
use facts it was given, must not be sensitive, and must never gossip about why a salesperson left.
Anything that fails is replaced by a plain explanation built from the signals.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from app.handoff import _DEPARTURE
from app.memory import sensitive_type
from app.meta_client import MetaBadResponse, MetaClient
from app.planner import Fact, PlanInputError, _text, mentioned_facts, parse_facts

MAX_CUSTOMERS = 300
MAX_FACTS_EACH = 12
MAX_OVERDUE_EACH = 10
DEFAULT_TOP = 8
MAX_TOP = 15
HIGH = 50
MEDIUM = 20
LIFE_EVENT_WINDOW = 45  # days ahead

KINDS = {
    "day1": "day-after check-in",
    "card": "thank-you card",
    "day30": "30-day call",
    "day90": "first service visit",
    "birthday": "birthday",
    "anniversary": "purchase anniversary",
    "life_event": "life-event check-in",
    "handoff_intro": "introduction from the new salesperson",
}
_MONTHS = {
    m: n
    for n, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.I)


@dataclass
class Overdue:
    kind: str
    due: dt.date


@dataclass
class Customer:
    ref: str
    name: str
    salesperson: str
    salesperson_active: bool
    vehicle: str
    delivered: dt.date | None
    last_contact: dt.date | None
    overdue: list[Overdue]
    facts: list[Fact]
    signals: list[dict] = field(default_factory=list)
    score: int = 0

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


def _date(raw, key: str, required: bool = False) -> dt.date | None:
    value = raw.get(key) or ""
    if not value:
        if required:
            raise PlanInputError(f"'{key}' is required.")
        return None
    if not isinstance(value, str):
        raise PlanInputError(f"'{key}' must look like 2026-10-27.")
    try:
        day = dt.date.fromisoformat(value[:10])
    except ValueError as exc:
        raise PlanInputError(f"'{key}' must look like 2026-10-27.") from exc
    if not 2000 <= day.year <= 2100:
        raise PlanInputError(f"'{key}' is out of range.")
    return day


def parse_input(data: dict) -> tuple[str, dt.date, int, bool, list[Customer]]:
    store = _text(data, "store_name", 60, required=False) or "Demo Motors"
    as_of = _date(data, "as_of", required=True)
    top = data.get("top", DEFAULT_TOP)
    if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= MAX_TOP:
        raise PlanInputError(f"'top' must be a whole number from 1 to {MAX_TOP}.")
    use_ai = data.get("ai", True)
    if not isinstance(use_ai, bool):
        raise PlanInputError("'ai' must be true or false.")

    raw_customers = data.get("customers")
    if not isinstance(raw_customers, list) or not raw_customers:
        raise PlanInputError("'customers' must be a non-empty list.")
    if len(raw_customers) > MAX_CUSTOMERS:
        raise PlanInputError(f"Send up to {MAX_CUSTOMERS} customers at a time.")

    customers, refs = [], set()
    for n, raw in enumerate(raw_customers, start=1):
        if not isinstance(raw, dict):
            raise PlanInputError("Each customer must be an object.")
        ref = _text(raw, "ref", 40, required=False) or f"C{n}"
        if ref in refs:
            raise PlanInputError(f"Customer ref '{ref}' is used twice.")
        refs.add(ref)
        overdue = []
        raw_overdue = raw.get("overdue", [])
        if not isinstance(raw_overdue, list):
            raise PlanInputError("'overdue' must be a list.")
        for item in raw_overdue[:MAX_OVERDUE_EACH]:
            if not isinstance(item, dict) or item.get("kind") not in KINDS:
                raise PlanInputError(f"Each overdue item needs a kind: {', '.join(KINDS)}.")
            overdue.append(Overdue(item["kind"], _date(item, "due_date", required=True)))
        raw_facts = raw.get("facts", [])
        if not isinstance(raw_facts, list):
            raise PlanInputError("'facts' must be a list.")
        active = raw.get("salesperson_active", True)
        if not isinstance(active, bool):
            raise PlanInputError("'salesperson_active' must be true or false.")
        customers.append(
            Customer(
                ref=ref,
                name=_text(raw, "customer_name", 80),
                salesperson=_text(raw, "salesperson", 60, required=False),
                salesperson_active=active,
                vehicle=_text(raw, "vehicle", 80, required=False) or "vehicle",
                delivered=_date(raw, "delivery_date"),
                last_contact=_date(raw, "last_contact_date"),
                overdue=overdue,
                facts=parse_facts({"facts": raw_facts[:MAX_FACTS_EACH]}),
            )
        )
    return store, as_of, top, use_ai, customers


def _upcoming_month(timing: str, as_of: dt.date) -> dt.date | None:
    match = _MONTH_RE.search(timing or "")
    if not match:
        return None
    month = _MONTHS[match[1].lower()]
    start = dt.date(as_of.year, month, 1)
    if start < as_of.replace(day=1):
        start = dt.date(as_of.year + 1, month, 1)
    return start


def score(customer: Customer, as_of: dt.date) -> None:
    """Find the reasons a customer needs attention. Pure code, no model call."""
    signals: list[dict] = []

    def add(kind: str, text: str, points: int, fact_id: str = "") -> None:
        signals.append({"id": f"S{len(signals) + 1}", "kind": kind, "text": text,
                        "points": points, "fact_id": fact_id})  # fmt: skip

    if not customer.salesperson or not customer.salesperson_active:
        who = f"{customer.salesperson} has left" if customer.salesperson else "No salesperson"
        add("no_salesperson", f"{who}; nobody owns this relationship", 40)

    late = sorted((o for o in customer.overdue if o.due < as_of), key=lambda o: o.due)
    if late:
        oldest = late[0]
        days = (as_of - oldest.due).days
        what = KINDS[oldest.kind]
        more = f" and {len(late) - 1} more" if len(late) > 1 else ""
        add("overdue", f"{what.capitalize()} is {days} days overdue{more}",
            10 * len(late) + min(days, 60))  # fmt: skip
        if customer.delivered and (as_of - customer.delivered).days <= 45:
            add("new_customer", "Bought in the last 45 days, so first impressions are at stake", 10)

    last = customer.last_contact or customer.delivered
    if last:
        quiet = (as_of - last).days
        if quiet > 180:
            add("silence", f"No contact in {quiet // 30} months", 30)
        elif quiet > 90:
            add("silence", f"No contact in {quiet} days", 15)

    for f in customer.facts:
        if f.category != "life_event" or f.certainty != "stated":
            continue
        when = _upcoming_month(f.timing, as_of)
        if not when:
            continue
        this_month = (when.year, when.month) == (as_of.year, as_of.month)
        if this_month or 0 <= (when - as_of).days <= LIFE_EVENT_WINDOW:
            add("life_event", f"Coming up in {when.strftime('%B')}: {f.fact}", 15, f.id)

    customer.signals = signals
    customer.score = sum(s["points"] for s in signals)


def level(points: int) -> str:
    return "high" if points >= HIGH else "medium" if points >= MEDIUM else ""


RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "customers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "why": {"type": "string"},
                    "uses_signals": {"type": "array", "items": {"type": "string"}},
                    "next_step": {"type": "string"},
                    "owner": {"type": "string"},
                    "opener": {"type": "string"},
                    "uses_facts": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "ref",
                    "why",
                    "uses_signals",
                    "next_step",
                    "owner",
                    "opener",
                    "uses_facts",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "customers"],
    "additionalProperties": False,
}

RISK_SYSTEM = """You write a short weekly brief for a car dealership sales manager about customer \
relationships that need attention. Code has already found the customers and the signals (with ids) \
that explain why. You explain and suggest; you never add new reasons.

For each customer:
- why: one or two plain sentences, based only on the signals given. List the signal ids you used \
in uses_signals.
- next_step: one concrete action for this week (who does what, and how: call, text, card, visit). \
If nobody owns the relationship, the next step is to assign a salesperson.
- owner: who should do it: the customer's salesperson, or "manager" if they have no salesperson.
- opener: one warm, specific line the salesperson could open with, using at most one personal \
fact. List any fact ids used in uses_facts. Leave "" if there are no facts.
headline: one sentence summarising the week for the manager.

Rules:
- Use only the signals and facts given. Never invent details.
- Never say or hint why a salesperson left, and never speak badly of anyone.
- Never mention money, prices, payments, credit, health, a death, legal matters or other sensitive \
topics.
- Be practical and kind. This is about keeping relationships, not pushing sales."""


def _fallback_step(c: Customer) -> tuple[str, str]:
    kinds = {s["kind"] for s in c.signals}
    if "no_salesperson" in kinds:
        return "Assign a salesperson and send a warm introduction this week.", "manager"
    if "overdue" in kinds:
        oldest = sorted(c.overdue, key=lambda o: o.due)[0]
        return f"Do the overdue {KINDS[oldest.kind]} this week.", c.salesperson or "manager"
    if "life_event" in kinds:
        return "Send a short personal check-in about what's coming up.", c.salesperson or "manager"
    return "Reach out with a friendly check-in, no sales pitch.", c.salesperson or "manager"


def _bad(text: str) -> bool:
    return bool(sensitive_type(text) or _DEPARTURE.search(text))


def _as_plan(c: Customer, store: str):
    """Reuse the planner's fact-mention check, which works on a PlanInput-shaped object."""
    from app.planner import PlanInput

    return PlanInput(
        customer_name=c.name,
        vehicle=c.vehicle,
        salesperson_name=c.salesperson or "manager",
        store_name=store,
        delivery_date=c.delivered or dt.date(2000, 1, 1),
        birthday=None,
        facts=c.facts,
    )


async def brief(
    meta: MetaClient, store: str, as_of: dt.date, top: int, use_ai: bool, customers: list[Customer]
) -> dict:
    for c in customers:
        score(c, as_of)
    flagged = sorted((c for c in customers if level(c.score)), key=lambda c: (-c.score, c.name))
    chosen = flagged[:top]

    totals = {
        "customers_checked": len(customers),
        "needs_attention": len(flagged),
        "high": sum(1 for c in flagged if level(c.score) == "high"),
        "no_salesperson": sum(
            1 for c in customers if any(s["kind"] == "no_salesperson" for s in c.signals)
        ),
        "overdue_follow_ups": sum(sum(1 for o in c.overdue if o.due < as_of) for c in customers),
        "written_up": len(chosen),
    }
    checks = {"explained_by_ai": 0, "replaced_with_plain_explanation": 0, "openers_dropped": 0}

    drafts: dict[str, dict] = {}
    headline = ""
    usage, latency, model = {}, 0, ""
    if use_ai and chosen:
        lines = []
        for c in chosen:
            sig = "; ".join(f"{s['id']} {s['text']}" for s in c.signals)
            facts = "; ".join(f"{f.id} {f.fact}" for f in c.facts) or "(none)"
            owner = c.salesperson if c.salesperson and c.salesperson_active else "(none)"
            lines.append(
                f"ref {c.ref}: {c.name}, {c.vehicle}, salesperson {owner}\n"
                f"  signals: {sig}\n  facts: {facts}"
            )
        user = f"Store: {store}\nWeek of: {as_of.isoformat()}\n\n" + "\n\n".join(lines)
        data, result = await meta.chat_json(
            [{"role": "system", "content": RISK_SYSTEM}, {"role": "user", "content": user}],
            RISK_SCHEMA,
            "risk_brief",
            reasoning_effort="low",
        )
        if not isinstance(data.get("customers"), list):
            raise MetaBadResponse("risk brief missing customers")
        for item in data["customers"]:
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                drafts.setdefault(item["ref"], item)
        headline = " ".join(str(data.get("headline", "")).split())[:240]
        usage, latency, model = result.usage, result.latency_ms, result.model

    if not headline or _bad(headline):
        ns, od = totals["no_salesperson"], totals["overdue_follow_ups"]
        headline = (
            f"{totals['needs_attention']} of {totals['customers_checked']} customers need "
            f"attention this week; {ns} {'has' if ns == 1 else 'have'} no salesperson and "
            f"{od} follow-up{'' if od == 1 else 's'} {'is' if od == 1 else 'are'} overdue."
        )

    out = []
    for rank, c in enumerate(chosen, start=1):
        raw = drafts.get(c.ref, {})
        signal_ids = {s["id"] for s in c.signals}
        cited = [s for s in raw.get("uses_signals", []) if s in signal_ids] if raw else []
        why = " ".join(str(raw.get("why", "")).split())[:300]
        step = " ".join(str(raw.get("next_step", "")).split())[:200]
        owner = " ".join(str(raw.get("owner", "")).split())[:60]
        fallback_step, fallback_owner = _fallback_step(c)
        used_ai = bool(raw) and bool(why) and bool(cited) and not _bad(why)
        if used_ai:
            checks["explained_by_ai"] += 1
        else:
            if use_ai:
                checks["replaced_with_plain_explanation"] += 1
            why = ". ".join(s["text"] for s in c.signals) + "."
            cited = sorted(signal_ids, key=lambda s: int(s[1:]))
        if not used_ai or not step or _bad(step):
            step = fallback_step
        active = c.salesperson.lower() if c.salesperson and c.salesperson_active else ""
        allowed_owners = {fallback_owner.lower(), "manager", active}
        if not used_ai or owner.lower() not in allowed_owners - {""}:
            owner = fallback_owner

        opener = " ".join(str(raw.get("opener", "")).split())[:240] if used_ai else ""
        known = {f.id: f for f in c.facts}
        refs = [
            {"id": fid, "fact": known[fid].fact}
            for fid in dict.fromkeys(raw.get("uses_facts", []) if used_ai else [])
            if isinstance(fid, str) and fid in known
        ]
        if opener:
            refs = mentioned_facts(refs, opener, _as_plan(c, store))
            if _bad(opener) or (raw.get("uses_facts") and not refs):
                checks["openers_dropped"] += 1
                opener, refs = "", []

        out.append(
            {
                "rank": rank,
                "ref": c.ref,
                "customer": c.name,
                "salesperson": c.salesperson,
                "salesperson_active": c.salesperson_active,
                "level": level(c.score),
                "score": c.score,
                "signals": [
                    {k: s[k] for k in ("id", "kind", "text")} | {"cited": s["id"] in cited}
                    for s in c.signals
                ],
                "why": why,
                "next_step": step,
                "owner": owner,
                "opener": opener,
                "uses_facts": refs,
                "explained_by": "ai" if used_ai else "code",
            }
        )

    return {
        "store": store,
        "as_of": as_of.isoformat(),
        "headline": headline,
        "totals": totals,
        "customers": out,
        "checks": checks,
        "usage": usage,
        "latency_ms": latency,
        "model": model,
    }
