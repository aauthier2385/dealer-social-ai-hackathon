"""Feature 1 tests: voice note -> relationship memory. All Meta calls are faked."""

import io
import json
import logging
import unittest
import wave

from app import memory
from app.main import create_app
from tests.helpers import FAKE_KEY, TEST_TOKEN, call, settings

TRANSCRIPT = {
    "sessionId": "test",
    "audioDurationMs": 62000,
    "transcript": "unused when turns exist",
    "turns": [
        {"speaker": "A", "startMs": 0, "transcript": "So tell me about the Carter deal."},
        {
            "speaker": "B",
            "startMs": 2100,
            "transcript": (
                "Mike Carter took the blue F-150 and traded his 2018 Silverado. "
                "His daughter Emma turns sixteen in March. "
                "His wife has been in and out of the hospital lately. "
                "He paid about 52 grand out the door. "
                "I think his brother-in-law might be looking for a truck too. "
                "He said text him, don't call, he's on the saw all day."
            ),
        },
    ],
}


def fact(about, category, text, sid, quote, certainty="stated", timing=""):
    return {
        "about": about,
        "category": category,
        "fact": text,
        "source_id": sid,
        "quote": quote,
        "certainty": certainty,
        "timing": timing,
    }


EXTRACTION = {
    "summary": "Mike Carter bought a blue F-150 and traded a 2018 Silverado.",
    "people": [
        {
            "name": "Mike Carter",
            "relationship": "customer",
            "related_to": "",
            "vehicle_purchased": "blue F-150",
            "vehicle_traded": "2018 Silverado",
        },
        {
            "name": "Emma",
            "relationship": "family_member",
            "related_to": "Mike Carter",
            "vehicle_purchased": "",
            "vehicle_traded": "",
        },
    ],
    "facts": [
        # 0: good fact
        fact("Mike Carter", "vehicle", "Bought a blue F-150", "S2", "took the blue F-150"),
        # 1: good fact with timing
        fact(
            "Emma",
            "life_event",
            "Daughter Emma turns 16",
            "S3",
            "His daughter Emma turns sixteen in March",
            timing="March",
        ),
        # 2: hallucinated: quote is not in the transcript
        fact("Mike Carter", "hobbies", "Loves bass fishing", "S3", "goes bass fishing every May"),
        # 3: model correctly flags sensitive
        fact("Mike Carter", "sensitive", "health detail", "S4", "in and out of the hospital"),
        # 4: deal numbers the model should not have extracted: caught by the code screen
        fact("Mike Carter", "personal", "Paid about 52 grand", "S5", "paid about 52 grand"),
        # 5: uncertain
        fact(
            "Mike Carter",
            "personal",
            "Brother-in-law may be shopping for a truck",
            "S6",
            "his brother-in-law might be looking for a truck",
            certainty="uncertain",
        ),
        # 6: cites the wrong sentence id but the quote exists elsewhere: id gets corrected
        fact(
            "Mike Carter",
            "contact_preference",
            "Prefers texts over calls",
            "S1",
            "text him, don't call",
        ),
        # 7: judge will reject this one (overstated)
        fact("Mike Carter", "work", "Owns a sawmill", "S7", "he's on the saw all day"),
    ],
}


def judge_all_supported(candidates, overrides=None):
    overrides = overrides or {}
    out = []
    for c in candidates:
        v = {
            "index": c["index"],
            "verdict": "supported",
            "sensitive_type": "",
            "certainty": c["certainty"],
            "reason": "stated",
        }
        v.update(overrides.get(c["fact"], {}))
        out.append(v)
    return {"verdicts": out}


class FakeMetaRouter:
    """Fakes /asr/transcribe and /chat/completions, answering by schema name."""

    def __init__(self, transcription=None, extraction=None, judge=None):
        self.transcription = TRANSCRIPT if transcription is None else transcription
        self.extraction = EXTRACTION if extraction is None else extraction
        self.judge = judge  # callable(candidates) -> dict, or None for "all supported"
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if url.endswith("/asr/transcribe"):
            return 200, {}, json.dumps(self.transcription).encode()
        request = json.loads(body)
        name = request["response_format"]["json_schema"]["name"]
        if name == "relationship_memory":
            content = self.extraction
        else:
            listing = request["messages"][1]["content"].split("Candidate facts:\n", 1)[1]
            candidates = json.loads(listing)
            content = (self.judge or judge_all_supported)(candidates)
        reply = {
            "model": "muse-spark-1.3",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
            "usage": {
                "prompt_tokens": 400,
                "completion_tokens": 120,
                "total_tokens": 520,
                "completion_tokens_details": {"reasoning_tokens": 60},
            },
        }
        return 200, {}, json.dumps(reply).encode()


def make_wav(seconds=3.0, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buffer.getvalue()


def upload(app, wav, token=TEST_TOKEN, content_type="audio/wav", extra=None):
    headers = {"content-type": content_type}
    if token:
        headers["authorization"] = f"Bearer {token}"
    headers.update(extra or {})
    return call(app, "POST", "/v1/memory/voice-note", headers=headers, body=wav)


class MemoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeMetaRouter()
        self.app = create_app(settings(), self.meta)
        self.response = upload(self.app, make_wav())
        self.body = self.response.json()
        self.facts = {f["fact"]: f for f in self.body["facts"]}

    def test_returns_verified_facts_with_sources(self):
        self.assertEqual(self.response.status, 200)
        f = self.facts["Bought a blue F-150"]
        self.assertEqual(f["source"]["sentence_id"], "S2")
        self.assertEqual(f["source"]["speaker"], "B")
        self.assertEqual(f["quote"], "took the blue F-150")
        self.assertEqual(self.facts["Daughter Emma turns 16"]["timing"], "March")
        self.assertEqual(self.body["checks"]["kept"], len(self.body["facts"]))

    def test_made_up_fact_is_dropped_by_code_check(self):
        self.assertNotIn("Loves bass fishing", self.facts)
        self.assertEqual(self.body["checks"]["quote_not_found"], 1)

    def test_wrong_sentence_id_is_corrected_to_the_real_source(self):
        self.assertEqual(self.facts["Prefers texts over calls"]["source"]["sentence_id"], "S7")

    def test_uncertain_stays_uncertain(self):
        f = self.facts["Brother-in-law may be shopping for a truck"]
        self.assertEqual(f["certainty"], "uncertain")

    def test_sensitive_details_are_withheld_and_redacted(self):
        text = self.response.text.lower()
        for secret in ["hospital", "52 grand", "paid about"]:
            self.assertNotIn(secret, text)
        kinds = sorted(w["type"] for w in self.body["withheld"])
        self.assertEqual(kinds, ["financial", "health"])
        redacted = {t["id"]: t for t in self.body["transcript"] if t["redacted"]}
        self.assertEqual(set(redacted), {"S4", "S5"})
        self.assertEqual(redacted["S5"]["text"], "[withheld: financial detail]")

    def test_people_and_usage_are_reported(self):
        names = [p["name"] for p in self.body["people"]]
        self.assertEqual(names, ["Mike Carter", "Emma"])
        self.assertEqual(self.body["people"][0]["vehicle_traded"], "2018 Silverado")
        self.assertEqual(self.body["usage"]["total_tokens"], 1040)  # extraction + judge
        self.assertEqual(self.body["usage"]["reasoning_tokens"], 120)
        self.assertEqual(self.body["models"]["transcription"], "muse-voice-transcribe-1.0")

    def test_sends_the_right_requests_to_meta(self):
        transcribe, extract, judge = self.meta.calls
        self.assertEqual(transcribe["url"], "https://api.meta.ai/v1/asr/transcribe")
        self.assertEqual(transcribe["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertIn("multipart/form-data; boundary=", transcribe["headers"]["Content-Type"])
        self.assertIn(b'"mode": "DIARIZATION"', transcribe["body"])
        self.assertIn(b'"model": "muse-voice-transcribe-1.0"', transcribe["body"])
        self.assertIn(b'"audioEncoding": "WAV"', transcribe["body"])
        self.assertIn(b"RIFF", transcribe["body"])
        request = json.loads(extract["body"])
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertIn("[S3] (B)  His daughter Emma", request["messages"][1]["content"])
        self.assertTrue(judge["url"].endswith("/chat/completions"))


class JudgeTests(unittest.TestCase):
    def test_judge_rejection_drops_the_fact(self):
        def judge(candidates):
            return judge_all_supported(candidates, {"Owns a sawmill": {"verdict": "not_supported"}})

        body = upload(create_app(settings(), FakeMetaRouter(judge=judge)), make_wav()).json()
        self.assertNotIn("Owns a sawmill", [f["fact"] for f in body["facts"]])
        self.assertEqual(body["checks"]["judge_rejected"], 1)

    def test_missing_verdicts_fail_closed(self):
        body = upload(
            create_app(settings(), FakeMetaRouter(judge=lambda c: {"verdicts": []})), make_wav()
        ).json()
        self.assertEqual(body["facts"], [])
        self.assertGreater(body["checks"]["judge_rejected"], 0)

    def test_judge_can_withhold_and_lower_certainty_but_not_raise_it(self):
        def judge(candidates):
            return judge_all_supported(
                candidates,
                {
                    "Bought a blue F-150": {"certainty": "uncertain"},
                    "Brother-in-law may be shopping for a truck": {"certainty": "stated"},
                    "Prefers texts over calls": {
                        "verdict": "sensitive",
                        "sensitive_type": "other",
                    },
                },
            )

        body = upload(create_app(settings(), FakeMetaRouter(judge=judge)), make_wav()).json()
        facts = {f["fact"]: f for f in body["facts"]}
        self.assertEqual(facts["Bought a blue F-150"]["certainty"], "uncertain")
        self.assertEqual(
            facts["Brother-in-law may be shopping for a truck"]["certainty"], "uncertain"
        )
        self.assertNotIn("Prefers texts over calls", facts)


class UploadValidationTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeMetaRouter()
        self.app = create_app(settings(), self.meta)

    def test_requires_token(self):
        self.assertEqual(upload(self.app, make_wav(), token=None).status, 401)
        self.assertEqual(self.meta.calls, [])

    def test_rejects_bad_audio_before_calling_meta(self):
        cases = [
            (b"not audio at all" * 10, "audio/wav", 400),
            (make_wav(channels=2), "audio/wav", 400),
            (make_wav(rate=8000), "audio/wav", 400),
            (make_wav(width=1), "audio/wav", 400),
            (make_wav(seconds=0.3), "audio/wav", 400),
            (make_wav(seconds=301, rate=16000), "audio/wav", 400),
            (make_wav(), "audio/mp4", 415),
        ]
        for data, content_type, expected in cases:
            r = upload(self.app, data, content_type=content_type)
            self.assertEqual(r.status, expected, (content_type, len(data)))
        self.assertEqual(self.meta.calls, [])

    def test_oversized_upload_is_refused(self):
        r = upload(self.app, make_wav(seconds=320, rate=24000))
        self.assertEqual(r.status, 413)

    def test_silence_returns_no_speech(self):
        meta = FakeMetaRouter(transcription={"transcript": "", "turns": []})
        r = upload(create_app(settings(), meta), make_wav())
        self.assertEqual(r.status, 422)
        self.assertEqual(r.json()["error"], "no_speech")
        self.assertEqual(len(meta.calls), 1)  # no chat calls spent on silence

    def test_voice_rate_limit(self):
        app = create_app(settings(VOICE_LIMIT_PER_CLIENT_PER_MINUTE="1"), FakeMetaRouter())
        self.assertEqual(upload(app, make_wav()).status, 200)
        self.assertEqual(upload(app, make_wav()).status, 429)

    def test_meta_outage_is_reported_cleanly(self):
        def broken(url, headers, body, timeout, max_bytes):
            return 503, {"Retry-After": "20"}, b"{}"

        r = upload(create_app(settings(), broken), make_wav())
        self.assertEqual(r.status, 503)
        self.assertEqual(r.headers["retry-after"], "20")


class PrivacyTests(unittest.TestCase):
    def test_transcript_and_facts_are_never_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            upload(create_app(settings(), FakeMetaRouter()), make_wav())
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        logs = stream.getvalue()
        self.assertIn("meta_transcribe status=200", logs)
        for secret in ["Emma", "Carter", "hospital", "Silverado", FAKE_KEY, TEST_TOKEN]:
            self.assertNotIn(secret, logs)


class UnitTests(unittest.TestCase):
    def test_sentence_splitting_keeps_speakers(self):
        sentences = memory.split_sentences(TRANSCRIPT)
        self.assertEqual(sentences[0].speaker, "A")
        self.assertEqual(sentences[2].text, "His daughter Emma turns sixteen in March.")
        self.assertEqual(sentences[2].id, "S3")

    def test_falls_back_to_plain_transcript(self):
        sentences = memory.split_sentences({"transcript": "One. Two!"})
        self.assertEqual([s.text for s in sentences], ["One.", "Two!"])

    def test_quote_matching_ignores_case_and_punctuation(self):
        sentences = memory.split_sentences(TRANSCRIPT)
        found = memory.locate_quote("HIS DAUGHTER emma, turns sixteen", "S3", sentences)
        self.assertEqual(found.id, "S3")
        self.assertIsNone(memory.locate_quote("Emma", "S3", sentences))  # too short to verify
        self.assertIsNone(memory.locate_quote("Emma turns seventeen", "S3", sentences))

    def test_sensitive_screen(self):
        cases = {
            "his wife is in the hospital": "health",
            "payments around 650 a month": "financial",
            "paid $48,000": "financial",
            "goes to church every Sunday": "religion",
            "just went through a divorce": "relationship",
            "her lease is up next summer": "",
            "his dog Duke rides in the truck bed": "",
        }
        for text, expected in cases.items():
            self.assertEqual(memory.sensitive_type(text), expected, text)


if __name__ == "__main__":
    unittest.main()


class SummaryAndDedupeTests(unittest.TestCase):
    """Regression tests from the first real run (Oct 27): a co-signing detail leaked via summary."""

    def test_judge_summary_is_used_when_clean(self):
        def judge(candidates):
            out = judge_all_supported(candidates)
            out["summary"] = "Mike bought a blue F-150; Emma turns 16 in March."
            return out

        body = upload(create_app(settings(), FakeMetaRouter(judge=judge)), make_wav()).json()
        self.assertEqual(body["summary"], "Mike bought a blue F-150; Emma turns 16 in March.")

    def test_summary_mentioning_withheld_money_detail_is_replaced(self):
        def judge(candidates):
            out = judge_all_supported(candidates)
            out["summary"] = "Mike bought the F-150 with his wife co-signing after a credit issue."
            return out

        body = upload(create_app(settings(), FakeMetaRouter(judge=judge)), make_wav()).json()
        self.assertNotIn("co-sign", body["summary"].lower())
        self.assertEqual(
            body["summary"], "Mike Carter bought a blue F-150 and traded a 2018 Silverado."
        )

    def test_extraction_summary_is_never_shown(self):
        extraction = dict(EXTRACTION, summary="LEAKED wife co-signed the loan")
        body = upload(
            create_app(settings(), FakeMetaRouter(extraction=extraction)), make_wav()
        ).json()
        self.assertNotIn("LEAKED", json.dumps(body))

    def test_cosigning_is_treated_as_financial(self):
        for text in ["his wife is co-signing", "she cosigned for him", "the credit application"]:
            self.assertEqual(memory.sensitive_type(text), "financial", text)

    def test_withheld_counts_each_sentence_once(self):
        extraction = dict(EXTRACTION)
        extraction["facts"] = EXTRACTION["facts"] + [
            fact("Mike Carter", "sensitive", "health detail", "S4", "in and out of the hospital")
        ]
        body = upload(
            create_app(settings(), FakeMetaRouter(extraction=extraction)), make_wav()
        ).json()
        self.assertEqual(sorted(w["source_id"] for w in body["withheld"]), ["S4", "S5"])
        self.assertEqual(body["checks"]["withheld"], 2)


class SeverityOrderTests(unittest.TestCase):
    def test_most_serious_category_wins(self):
        # From the Oct 27 real run: one sentence held both a co-signer and a DUI.
        text = "his wife cosigned with him and his daughter went to jail for a DUI"
        self.assertEqual(memory.sensitive_type(text), "legal")
        self.assertEqual(memory.sensitive_type("cosigned after his surgery"), "health")


QA_TRANSCRIPT = {
    "turns": [
        {"speaker": "A", "startMs": 0, "transcript": "Does she work or is she retired?"},
        {"speaker": "B", "startMs": 3000, "transcript": "She's retired."},
        {"speaker": "A", "startMs": 5000, "transcript": "Was she married?"},
        {
            "speaker": "B",
            "startMs": 7000,
            "transcript": "She's a widow, her husband died last year.",
        },
        {"speaker": "A", "startMs": 9000, "transcript": "Wasn't he moving to Florida?"},
        {"speaker": "B", "startMs": 11000, "transcript": "I don't know anything about that."},
    ]
}
QA_EXTRACTION = {
    "people": [],
    "facts": [fact("Madeline Parks", "work", "Retired", "S2", "She's retired")],
}


class QuestionAnswerTests(unittest.TestCase):
    """From the Oct 27 real runs: debriefs are mostly a question followed by a short answer."""

    def setUp(self):
        self.meta = FakeMetaRouter(transcription=QA_TRANSCRIPT, extraction=QA_EXTRACTION)
        self.body = upload(create_app(settings(), self.meta), make_wav()).json()

    def test_short_answer_fact_is_kept_with_its_question_as_context(self):
        self.assertEqual([f["fact"] for f in self.body["facts"]], ["Retired"])
        judge_request = json.loads(self.meta.calls[2]["body"])
        listing = judge_request["messages"][1]["content"].split("Candidate facts:\n", 1)[1]
        context = json.loads(listing)[0]["context"]
        self.assertEqual(context, ["[S1] (A) Does she work or is she retired?"])

    def test_self_check_uses_minimal_reasoning_and_extraction_uses_low(self):
        extract, judge = (json.loads(c["body"]) for c in self.meta.calls[1:3])
        self.assertEqual(extract["reasoning_effort"], "low")
        self.assertEqual(judge["reasoning_effort"], "minimal")

    def test_bereavement_lines_are_redacted_including_the_question(self):
        transcript = {t["id"]: t for t in self.body["transcript"]}
        self.assertTrue(transcript["S4"]["redacted"])
        self.assertEqual(transcript["S4"]["text"], "[withheld: bereavement detail]")
        self.assertNotIn("widow", json.dumps(self.body).lower())
        self.assertNotIn("died", json.dumps(self.body).lower())

    def test_bereavement_screen(self):
        for text in [
            "she's a widow",
            "her husband died last year",
            "he passed away",
            "his late wife",
        ]:
            self.assertEqual(memory.sensitive_type(text), "bereavement", text)
        for text in ["his dead battery", "a die-hard Ford fan"]:
            self.assertEqual(memory.sensitive_type(text), "", text)


class MixedSentenceTests(unittest.TestCase):
    """From the Oct 27 Mark Stevenson run: safe facts were lost when they shared a sentence with a
    sensitive detail."""

    def test_safe_fact_survives_a_mixed_sentence_but_the_sentence_is_redacted(self):
        transcription = {
            "turns": [
                {"speaker": "A", "startMs": 0, "transcript": "What did he do for a living?"},
                {
                    "speaker": "B",
                    "startMs": 2000,
                    "transcript": "He had a lot of money, he's retired, he cuts down a lot of trees.",
                },
            ]
        }
        extraction = {
            "people": [],
            "facts": [
                fact("Mark Stevenson", "work", "Retired", "S2", "he's retired"),
                fact(
                    "Mark Stevenson",
                    "hobbies",
                    "Cuts down a lot of trees",
                    "S2",
                    "he cuts down a lot of trees",
                ),
            ],
        }
        meta = FakeMetaRouter(transcription=transcription, extraction=extraction)
        body = upload(create_app(settings(), meta), make_wav()).json()
        self.assertEqual(
            [f["fact"] for f in body["facts"]], ["Retired", "Cuts down a lot of trees"]
        )
        line = {t["id"]: t for t in body["transcript"]}["S2"]
        self.assertTrue(line["redacted"])
        self.assertNotIn("money", json.dumps(body).lower())

    def test_prompts_tell_both_passes_to_judge_the_fact_not_the_sentence(self):
        self.assertIn("quote only the safe words", memory.EXTRACT_SYSTEM)
        self.assertIn("Judge the fact and quote, not the rest of the sentence", memory.JUDGE_SYSTEM)


class SecondRealRunTests(unittest.TestCase):
    """From the second real run on Oct 27, after the first round of fixes."""

    transcription = {
        "turns": [
            {"speaker": "A", "startMs": 0, "transcript": "oh so back in September"},
            {"speaker": "B", "startMs": 2000, "transcript": "yes yes"},
            {"speaker": "A", "startMs": 3000, "transcript": "speaker_A|> gotcha"},
            {"speaker": "A", "startMs": 4000, "transcript": "<|endoftext|>"},
            {
                "speaker": "B",
                "startMs": 5000,
                "transcript": "she just said he passed a while back and then she bought a farm",
            },
            {"speaker": "B", "startMs": 7000, "transcript": "we were able to lower payment"},
        ]
    }
    extraction = {
        "people": [],
        "facts": [
            fact(
                "Carl Jeffers",
                "vehicle",
                "Wife's Trailblazer was bought in September",
                "S2",
                "yes yes",
            ),
            fact("Madeline Parks", "family", "Bought a farm", "S4", "she bought a farm"),
        ],
    }

    def setUp(self):
        meta = FakeMetaRouter(transcription=self.transcription, extraction=self.extraction)
        self.body = upload(create_app(settings(), meta), make_wav()).json()
        self.lines = {t["id"]: t for t in self.body["transcript"]}

    def test_transcription_artifacts_are_removed(self):
        self.assertEqual(self.lines["S3"]["text"], "gotcha")
        self.assertNotIn("speaker_A", json.dumps(self.body))
        self.assertNotIn("endoftext", json.dumps(self.body))
        self.assertEqual(len(self.body["transcript"]), 5)  # the artifact-only line is dropped

    def test_he_passed_is_bereavement_and_redacted(self):
        self.assertEqual(self.lines["S4"]["text"], "[withheld: bereavement detail]")
        self.assertEqual(memory.sensitive_type("her husband had passed"), "bereavement")
        self.assertEqual(memory.sensitive_type("he passed the inspection"), "bereavement")

    def test_withheld_notice_counts_every_redacted_line(self):
        self.assertEqual(
            [(w["source_id"], w["type"]) for w in self.body["withheld"]],
            [("S4", "bereavement"), ("S5", "financial")],
        )
        self.assertEqual(self.body["checks"]["withheld"], 2)

    def test_short_answer_shows_the_question_it_answered(self):
        facts = {f["fact"]: f for f in self.body["facts"]}
        self.assertEqual(
            facts["Wife's Trailblazer was bought in September"]["asked"], "oh so back in September"
        )
        self.assertEqual(facts["Bought a farm"]["asked"], "")  # same speaker before it: no question

    def test_summary_prompt_hedges_uncertain_facts(self):
        self.assertIn('Say "may" or "possibly"', memory.JUDGE_SYSTEM)


class ThirdRealRunTests(unittest.TestCase):
    """Fixes from the Oct 27 planner run: one dog counted as two, and a withheld question shown."""

    def test_withheld_question_is_not_shown_as_the_question(self):
        sent = memory.Sentence
        sentences = [
            sent("S1", "A", 0, "he sold his business for a lot of money"),
            sent("S2", "B", 1000, "he's retired"),
        ]
        self.assertEqual(
            memory._asked(sentences[1], sentences, {"S1": "financial"}, "he's retired"), ""
        )

    def test_named_pets_are_people_entries(self):
        relationships = memory.EXTRACTION_SCHEMA["properties"]["people"]["items"]["properties"][
            "relationship"
        ]["enum"]
        self.assertIn("pet", relationships)
        self.assertIn("never two separate animals", memory.EXTRACT_SYSTEM)
