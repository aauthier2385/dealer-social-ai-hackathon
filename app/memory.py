"""Feature 1: voice note -> relationship memory, where every fact is tied to its source.

Pipeline (each step can only remove facts, never add them):
  1. Muse Voice Transcribe turns the recording into speaker-labelled text.
  2. The transcript is split into numbered sentences [S1], [S2], ...
  3. Muse Spark extracts relationship facts with structured output. Each fact must cite one
     sentence and copy an exact quote from it.
  4. Code verifies every quote really appears in the transcript. Anything that does not is dropped.
  5. A privacy screen withholds sensitive details (health, money, religion, legal, ...).
  6. A second Muse Spark pass (the judge) re-checks each remaining fact against its sentence and
     drops anything not clearly stated. Missing verdicts count as "not supported" (fail closed).
  7. Sentences behind withheld details are redacted from the transcript that is returned.

Nothing here is stored or logged. The caller decides what to save.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from app.meta_client import MetaBadResponse, MetaClient

CATEGORIES = [
    "family",
    "pets",
    "hobbies",
    "work",
    "vehicle",
    "life_event",
    "contact_preference",
    "important_date",
    "personal",
    "sensitive",
]
SENSITIVE_TYPES = [
    "",
    "health",
    "financial",
    "religion",
    "politics",
    "legal",
    "immigration",
    "sexuality",
    "relationship",
    "other",
]

# Backup screen in code, so a model mistake cannot leak a sensitive detail. It errs on the side of
# withholding: a false alarm costs one lost fact, a miss costs a customer's trust.
_SENSITIVE_PATTERNS = {  # most serious first: a sentence gets the label of its first match
    "legal": r"\b(arrest\w*|jail|prison|dui|dwi|court|lawsuit|probation|lawyer|attorney)\b",
    "health": r"\b(hospital\w*|sick|illness|ill|cancer|surger(y|ies)|diagnos\w*|medication\w*|"
    r"medicine|chemo\w*|therap(y|ist)|rehab|disease|diabet\w*|pregnan\w*|disab\w*|injur\w*|"
    r"mental health|depress\w*|anxiety|passed away|funeral|heart attack|stroke)\b",
    "immigration": r"\b(immigra\w*|visa|green card|deport\w*|citizenship)\b",
    "relationship": r"\b(divorc\w*|separated|affair|custody)\b",
    "religion": r"\b(church|mosque|synagogue|religio\w*|pray\w*|bible)\b",
    "politics": r"\b(democrat\w*|republican\w*|election|politic\w*|voted|voting)\b",
    "financial": r"(\$\s?\d|\b\d[\d,]*\s?(dollars|bucks|grand|k)\b|\b\d{2,}\s?(a|per)\s?month\b|"
    r"\b(credit( score)?|bankrupt\w*|repo(ssess\w*)?|debt|loans?|apr|interest rate|"
    r"down payment|payments?|income|salary|paycheck|financ\w*|negative equity|upside down|"
    r"co-?sign\w*|credit app\w*|"
    r"owe[sd]?|behind on|money)\b)",
}
_SENSITIVE_RE = {k: re.compile(v, re.IGNORECASE) for k, v in _SENSITIVE_PATTERNS.items()}


def sensitive_type(text: str) -> str:
    """Returns the first sensitive category the text touches, or ''."""
    for kind, pattern in _SENSITIVE_RE.items():
        if pattern.search(text):
            return kind
    return ""


def _str(kind="string", **extra):
    return {"type": kind, **extra}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": _str(),
                    "relationship": _str(
                        enum=["customer", "family_member", "salesperson", "manager", "other"]
                    ),
                    "related_to": _str(),
                    "vehicle_purchased": _str(),
                    "vehicle_traded": _str(),
                },
                "required": [
                    "name",
                    "relationship",
                    "related_to",
                    "vehicle_purchased",
                    "vehicle_traded",
                ],
                "additionalProperties": False,
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "about": _str(),
                    "category": _str(enum=CATEGORIES),
                    "fact": _str(),
                    "source_id": _str(),
                    "quote": _str(),
                    "certainty": _str(enum=["stated", "uncertain"]),
                    "timing": _str(),
                },
                "required": [
                    "about",
                    "category",
                    "fact",
                    "source_id",
                    "quote",
                    "certainty",
                    "timing",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["people", "facts"],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": _str("integer"),
                    "verdict": _str(enum=["supported", "not_supported", "sensitive"]),
                    "sensitive_type": _str(enum=SENSITIVE_TYPES),
                    "certainty": _str(enum=["stated", "uncertain"]),
                    "reason": _str(),
                },
                "required": ["index", "verdict", "sensitive_type", "certainty", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": _str(),
    },
    "required": ["verdicts", "summary"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = """You turn a car salesperson's voice note into relationship memory for The Dealer \
Social, an app that helps dealerships stay in touch with customers for years.

The transcript is split into numbered sentences like "[S3] (A) text". Speakers are labelled A, B by \
the transcription. Work out from context who is the salesperson, manager and customer.

Extract RELATIONSHIP facts a good salesperson would want to remember: family members and their \
names, pets, hobbies, work, the vehicle bought and traded and how they use it, upcoming life events \
and when they happen, contact preferences, birthdays and anniversaries, and personal details that \
make a future conversation warm.

Rules:
- Only facts actually said. Never infer, guess or add detail.
- Every fact cites exactly one source_id and copies an exact quote (4 to 25 consecutive words) from \
that sentence, word for word.
- If the speaker sounds unsure ("I think", "maybe", "might", "not sure"), set certainty "uncertain".
- timing: when it happens, if said ("March", "next summer", "every May"); otherwise "".
- Never record deal numbers or money: prices, payments, credit, loans, income, debt.
- Sensitive details (health, money, religion, politics, legal trouble, immigration, sexuality, \
relationship trouble): list them with category "sensitive" and a generic fact such as "health \
detail" so they can be withheld. Never repeat the details.
- One fact per item, short, written for a salesperson reading it before a call.
- people: everyone named, with relationship and related_to. Use "" when unknown."""

JUDGE_SYSTEM = """You are a strict auditor for a customer-memory system. For each candidate fact, \
decide using ONLY its cited transcript sentence:
- "supported": the sentence clearly states the fact, and the fact adds nothing that was not said.
- "not_supported": the fact is not stated, exaggerates, adds detail, or cites the wrong sentence.
- "sensitive": the fact or its sentence involves health, money or deal numbers, religion, politics, \
legal trouble, immigration, sexuality or relationship trouble. Set sensitive_type.
certainty: "uncertain" if the sentence is hedged (I think, maybe, might, not sure, possibly), \
otherwise "stated".
Return exactly one verdict per candidate, by index. When in doubt, choose "not_supported".
Then write summary: one or two sentences a salesperson would read before calling the customer, \
using ONLY the facts you marked "supported". Never mention anything sensitive, money, financing, \
credit or co-signing."""


class NoSpeech(Exception):
    """The recording contained no words."""


@dataclass
class Sentence:
    id: str
    speaker: str
    start_ms: int
    text: str


_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(transcription: dict) -> list[Sentence]:
    turns = transcription.get("turns")
    if not isinstance(turns, list) or not turns:
        turns = [{"transcript": transcription.get("transcript", ""), "startMs": 0}]
    sentences: list[Sentence] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = turn.get("transcript") or turn.get("text") or ""
        if not isinstance(text, str):
            continue
        speaker = turn.get("speaker") if isinstance(turn.get("speaker"), str) else ""
        start = turn.get("startMs") if isinstance(turn.get("startMs"), int) else 0
        for piece in _SPLIT.split(text.strip()):
            piece = piece.strip()
            if piece:
                sentences.append(Sentence(f"S{len(sentences) + 1}", speaker[:8], start, piece))
    return sentences


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def locate_quote(quote: str, source_id: str, sentences: list[Sentence]) -> Sentence | None:
    """Find the sentence that really contains the quote: the cited one first, then any other."""
    needle = _norm(quote)
    if len(needle.split()) < 2:
        return None
    by_id = {s.id: s for s in sentences}
    cited = by_id.get(source_id)
    if cited and needle in _norm(cited.text):
        return cited
    for sentence in sentences:
        if needle in _norm(sentence.text):
            return sentence
    return None


def _transcript_lines(sentences: list[Sentence]) -> str:
    return "\n".join(
        f"[{s.id}] ({s.speaker})  {s.text}" if s.speaker else f"[{s.id}] {s.text}"
        for s in sentences
    )


def _add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


def _fallback_summary(people: list[dict]) -> str:
    for p in people:
        if p["relationship"] == "customer":
            line = p["name"]
            if p["vehicle_purchased"]:
                line += f" bought a {p['vehicle_purchased']}"
                if p["vehicle_traded"]:
                    line += f" and traded a {p['vehicle_traded']}"
            return line + "."
    return ""


async def remember(meta: MetaClient, wav: bytes, audio_seconds: float) -> dict:
    stages: dict[str, int] = {}
    usage: dict[str, int] = {}

    started = time.monotonic()
    transcription = await meta.transcribe(wav, mode="DIARIZATION")
    stages["transcribe_ms"] = int((time.monotonic() - started) * 1000)

    sentences = split_sentences(transcription)
    if not sentences:
        raise NoSpeech()
    lines = _transcript_lines(sentences)

    started = time.monotonic()
    extraction, result = await meta.chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Voice note transcript:\n{lines}"},
        ],
        EXTRACTION_SCHEMA,
        "relationship_memory",
        reasoning_effort="low",
    )
    stages["extract_ms"] = int((time.monotonic() - started) * 1000)
    _add_usage(usage, result.usage)

    raw_facts = extraction.get("facts")
    raw_people = extraction.get("people")
    if not isinstance(raw_facts, list) or not isinstance(raw_people, list):
        raise MetaBadResponse("extraction missing facts or people")

    checks = {"extracted": 0, "quote_not_found": 0, "withheld": 0, "judge_rejected": 0, "kept": 0}
    redact: dict[str, str] = {}
    candidates: list[dict] = []

    for raw in raw_facts:
        if not isinstance(raw, dict):
            continue
        checks["extracted"] += 1
        category = raw.get("category") if raw.get("category") in CATEGORIES else "personal"
        quote = str(raw.get("quote", ""))
        fact_text = str(raw.get("fact", "")).strip()
        sentence = locate_quote(quote, str(raw.get("source_id", "")), sentences)
        if sentence is None:
            checks["quote_not_found"] += 1
            continue
        kind = "other" if category == "sensitive" else ""
        kind = sensitive_type(f"{fact_text} {quote}") or kind
        if kind:
            redact.setdefault(sentence.id, kind)
            continue
        candidates.append(
            {
                "about": str(raw.get("about", "")).strip()[:80],
                "category": category,
                "fact": fact_text[:300],
                "timing": str(raw.get("timing", "")).strip()[:60],
                "certainty": "uncertain" if raw.get("certainty") == "uncertain" else "stated",
                "sentence": sentence,
                "quote": quote.strip()[:300],
            }
        )

    kept: list[dict] = []
    judge_summary = ""
    if candidates:
        started = time.monotonic()
        listing = json.dumps(
            [
                {
                    "index": i,
                    "fact": c["fact"],
                    "source_id": c["sentence"].id,
                    "quote": c["quote"],
                    "certainty": c["certainty"],
                }
                for i, c in enumerate(candidates)
            ],
            ensure_ascii=False,
        )
        judged, result = await meta.chat_json(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": f"Transcript:\n{lines}\n\nCandidate facts:\n{listing}",
                },
            ],
            JUDGE_SCHEMA,
            "fact_audit",
            reasoning_effort="low",
        )
        stages["judge_ms"] = int((time.monotonic() - started) * 1000)
        _add_usage(usage, result.usage)
        judge_summary = str(judged.get("summary", "")).strip()[:600]
        verdicts = {}
        for v in judged.get("verdicts", []) if isinstance(judged.get("verdicts"), list) else []:
            if isinstance(v, dict) and isinstance(v.get("index"), int):
                verdicts.setdefault(v["index"], v)

        for i, c in enumerate(candidates):
            verdict = verdicts.get(i, {})
            decision = verdict.get("verdict")
            if decision == "sensitive":
                kind = verdict.get("sensitive_type") if verdict.get("sensitive_type") else "other"
                redact.setdefault(c["sentence"].id, kind)
                continue
            if decision != "supported":  # includes a missing verdict: fail closed
                checks["judge_rejected"] += 1
                continue
            if verdict.get("certainty") == "uncertain":  # the judge may only lower certainty
                c["certainty"] = "uncertain"
            kept.append(c)

    # One withheld entry per sentence a sensitive fact came from.
    withheld = [{"type": kind, "source_id": sid} for sid, kind in redact.items()]
    checks["withheld"] = len(withheld)

    # Redact any other sentence the screen flags, even if no fact came from it.
    for s in sentences:
        kind = sensitive_type(s.text)
        if kind and s.id not in redact:
            redact[s.id] = kind

    facts_out = []
    for n, c in enumerate(kept, start=1):
        s = c["sentence"]
        facts_out.append(
            {
                "id": f"F{n}",
                "about": c["about"],
                "category": c["category"],
                "fact": c["fact"],
                "timing": c["timing"],
                "certainty": c["certainty"],
                "source": {"sentence_id": s.id, "speaker": s.speaker, "start_ms": s.start_ms},
                "quote": c["quote"],
            }
        )
    checks["kept"] = len(facts_out)

    people_out = []
    for p in raw_people:
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            continue

        def clean(value) -> str:
            text = str(value or "").strip()[:120]
            return "" if sensitive_type(text) else text

        people_out.append(
            {
                "name": str(p["name"]).strip()[:80],
                "relationship": p.get("relationship")
                if p.get("relationship")
                in EXTRACTION_SCHEMA["properties"]["people"]["items"]["properties"]["relationship"][
                    "enum"
                ]
                else "other",
                "related_to": clean(p.get("related_to")),
                "vehicle_purchased": clean(p.get("vehicle_purchased")),
                "vehicle_traded": clean(p.get("vehicle_traded")),
            }
        )

    # The summary comes from the judge, written from verified facts only. If it is missing or trips
    # the privacy screen, fall back to a plain line built from the people list.
    summary = judge_summary if kept and not sensitive_type(judge_summary) else ""
    if not summary:
        summary = _fallback_summary(people_out)

    return {
        "summary": summary,
        "people": people_out,
        "facts": facts_out,
        "withheld": withheld,
        "checks": checks,
        "transcript": [
            {
                "id": s.id,
                "speaker": s.speaker,
                "start_ms": s.start_ms,
                "text": f"[withheld: {redact[s.id]} detail]" if s.id in redact else s.text,
                "redacted": s.id in redact,
            }
            for s in sentences
        ],
        "audio_seconds": audio_seconds,
        "timing_ms": stages,
        "usage": usage,
        "models": {"transcription": meta.voice_model, "reasoning": meta.text_model},
    }
