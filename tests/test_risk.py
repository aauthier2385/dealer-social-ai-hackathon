"""Feature 6 tests: relationships-at-risk brief. Scoring is pure code; Meta calls are faked."""

import json
import unittest

from app import risk
from app.main import create_app
from tests.helpers import TEST_TOKEN, call, settings

AS_OF = "2026-11-02"

CUSTOMERS = [
    {  # salesperson left + overdue intro: high
        "ref": "c-mark",
        "customer_name": "Mark Stevenson",
        "salesperson": "Joey",
        "salesperson_active": False,
        "vehicle": "26 Ford Ranger Lariat",
        "delivery_date": "2026-10-27",
        "last_contact_date": "2026-10-28",
        "overdue": [{"kind": "card", "due_date": "2026-10-29"}],
        "facts": [
            {
                "id": "F1",
                "about": "Bell",
                "category": "pets",
                "fact": "Has a little Frenchie named Bell",
            },
        ],
    },
    {  # overdue 30-day call, new customer: medium
        "ref": "c-madeline",
        "customer_name": "Madeline Parks",
        "salesperson": "Jayden",
        "vehicle": "2024 Ford Escape Active",
        "delivery_date": "2026-09-28",
        "last_contact_date": "2026-09-29",
        "overdue": [{"kind": "day30", "due_date": "2026-10-28"}],
        "facts": [
            {
                "id": "F1",
                "about": "Madeline Parks",
                "category": "family",
                "fact": "Bought a farm next to her kids",
            },
        ],
    },
    {  # long silence + life event this month
        "ref": "c-mike",
        "customer_name": "Mike Carter",
        "salesperson": "Andrew",
        "vehicle": "blue F-150",
        "delivery_date": "2025-11-20",
        "last_contact_date": "2026-03-01",
        "facts": [
            {
                "id": "F1",
                "about": "Emma",
                "category": "life_event",
                "fact": "Daughter Emma turns 16",
                "timing": "November",
            },
            {
                "id": "F2",
                "about": "Mike Carter",
                "category": "personal",
                "fact": "Wife was in the hospital",
            },
        ],
    },
    {  # all good: not flagged
        "ref": "c-carl",
        "customer_name": "Carl Jeffers",
        "salesperson": "Tony",
        "vehicle": "2020 Hyundai Elantra",
        "delivery_date": "2026-10-27",
        "last_contact_date": "2026-10-28",
        "overdue": [{"kind": "day30", "due_date": "2026-11-26"}],  # not due yet
    },
]
REQUEST = {"store_name": "Demo Motors", "as_of": AS_OF, "customers": CUSTOMERS}

GOOD = {
    "headline": "Three relationships need attention, led by Joey's customers.",
    "customers": [
        {
            "ref": "c-mark",
            "why": "Mark's salesperson is no longer assigned and his thank-you card is late.",
            "uses_signals": ["S1", "S2"],
            "next_step": "Assign Mark to a salesperson and send the thank-you card this week.",
            "owner": "manager",
            "opener": "How's Bell liking the new Ranger?",
            "uses_facts": ["F1"],
        },
        {
            "ref": "c-madeline",
            "why": "Her 30-day call is overdue.",
            "uses_signals": ["S1"],
            "next_step": "Jayden calls Madeline this week.",
            "owner": "Jayden",
            "opener": "How's life on the farm?",
            "uses_facts": ["F1"],
        },
        {
            "ref": "c-mike",
            "why": "We haven't talked to Mike in months and Emma turns 16 this month.",
            "uses_signals": ["S1", "S2"],
            "next_step": "Andrew texts Mike about Emma's birthday.",
            "owner": "Andrew",
            "opener": "Isn't Emma turning 16 this month?",
            "uses_facts": ["F1"],
        },
    ],
}


class FakeRiskMeta:
    def __init__(self, reply=None):
        self.reply = GOOD if reply is None else reply
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        self.calls.append(json.loads(body))
        out = {
            "model": "muse-spark-1.3",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(self.reply)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300},
        }
        return 200, {}, json.dumps(out).encode()


def post(app, body, token=TEST_TOKEN):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return call(app, "POST", "/v1/risk/brief", headers=headers, body=json.dumps(body).encode())


def run(reply=None, request=None):
    meta = FakeRiskMeta(reply)
    r = post(create_app(settings(), meta), request or REQUEST)
    return r, meta


def reply_with(ref, **changes):
    reply = json.loads(json.dumps(GOOD))
    for c in reply["customers"]:
        if c["ref"] == ref:
            c.update(changes)
    return reply


class ScoringTests(unittest.TestCase):
    def test_ranking_and_levels_come_from_code(self):
        body = run()[0].json()
        self.assertEqual([c["ref"] for c in body["customers"]], ["c-mark", "c-mike", "c-madeline"])
        levels = {c["ref"]: c["level"] for c in body["customers"]}
        self.assertEqual(levels["c-mark"], "high")
        self.assertNotIn("c-carl", levels)  # nothing overdue yet, recent contact
        t = body["totals"]
        self.assertEqual(
            (
                t["customers_checked"],
                t["needs_attention"],
                t["no_salesperson"],
                t["overdue_follow_ups"],
            ),
            (4, 3, 1, 2),
        )

    def test_signals(self):
        body = run()[0].json()
        mark = body["customers"][0]
        self.assertEqual(
            [s["kind"] for s in mark["signals"]], ["no_salesperson", "overdue", "new_customer"]
        )
        self.assertIn("Joey has left", mark["signals"][0]["text"])
        self.assertIn("Thank-you card is 4 days overdue", mark["signals"][1]["text"])
        mike = {c["ref"]: c for c in body["customers"]}["c-mike"]
        self.assertEqual([s["kind"] for s in mike["signals"]], ["silence", "life_event"])
        self.assertIn("Coming up in November: Daughter Emma turns 16", mike["signals"][1]["text"])

    def test_life_event_window(self):
        as_of = risk.dt.date(2026, 11, 2)
        self.assertEqual(risk._upcoming_month("November", as_of), risk.dt.date(2026, 11, 1))
        self.assertEqual(risk._upcoming_month("in March", as_of), risk.dt.date(2027, 3, 1))
        self.assertIsNone(risk._upcoming_month("next summer", as_of))

    def test_top_limits_what_is_written_up(self):
        body = run(request=dict(REQUEST, top=1))[0].json()
        self.assertEqual(len(body["customers"]), 1)
        self.assertEqual(body["totals"]["needs_attention"], 3)
        self.assertEqual(body["totals"]["written_up"], 1)


class ExplanationTests(unittest.TestCase):
    def test_good_explanations_are_kept(self):
        body = run()[0].json()
        mark = body["customers"][0]
        self.assertEqual(mark["explained_by"], "ai")
        self.assertEqual(mark["owner"], "manager")
        self.assertEqual(mark["opener"], "How's Bell liking the new Ranger?")
        self.assertEqual([f["id"] for f in mark["uses_facts"]], ["F1"])
        self.assertEqual(body["checks"]["explained_by_ai"], 3)

    def test_uncited_or_gossipy_why_is_replaced_by_signals(self):
        for change in [
            {"uses_signals": []},
            {"uses_signals": ["S9"]},
            {"why": "Joey got fired, so Mark is on his own."},
            {"why": "Mark still owes on his loan."},
        ]:
            body = run(reply_with("c-mark", **change))[0].json()
            mark = body["customers"][0]
            self.assertEqual(mark["explained_by"], "code", change)
            self.assertIn("Joey has left", mark["why"])
            self.assertNotIn("fired", json.dumps(mark))
            self.assertEqual(mark["opener"], "")
            self.assertEqual(body["checks"]["replaced_with_plain_explanation"], 1)

    def test_owner_cannot_be_the_salesperson_who_left(self):
        mark = run(reply_with("c-mark", owner="Joey"))[0].json()["customers"][0]
        self.assertEqual(mark["owner"], "manager")

    def test_opener_must_mention_the_fact_it_cites(self):
        body = run(reply_with("c-madeline", opener="Hope you're doing well!", uses_facts=["F1"]))[
            0
        ].json()
        madeline = {c["ref"]: c for c in body["customers"]}["c-madeline"]
        self.assertEqual(madeline["opener"], "")
        self.assertEqual(body["checks"]["openers_dropped"], 1)

    def test_bad_headline_falls_back(self):
        body = run(dict(GOOD, headline="Joey quit and left a mess"))[0].json()
        self.assertEqual(
            body["headline"],
            "3 of 4 customers need attention this week; 1 has no salesperson and 2 follow-ups are overdue.",
        )

    def test_code_only_mode_makes_no_model_call(self):
        r, meta = run(request=dict(REQUEST, ai=False))
        body = r.json()
        self.assertEqual(meta.calls, [])
        self.assertEqual({c["explained_by"] for c in body["customers"]}, {"code"})
        self.assertEqual(body["checks"]["replaced_with_plain_explanation"], 0)
        self.assertTrue(body["customers"][0]["next_step"].startswith("Assign a salesperson"))

    def test_only_top_customers_and_no_sensitive_facts_reach_meta(self):
        r, meta = run(request=dict(REQUEST, top=2))
        prompt = meta.calls[0]["messages"][1]["content"]
        self.assertIn("ref c-mark", prompt)
        self.assertNotIn("ref c-madeline", prompt)  # ranked third
        self.assertNotIn("Carl", prompt)  # not at risk: never sent
        self.assertNotIn("hospital", prompt)
        self.assertEqual(meta.calls[0]["response_format"]["json_schema"]["name"], "risk_brief")


class InputTests(unittest.TestCase):
    def test_requires_token(self):
        self.assertEqual(
            post(create_app(settings(), FakeRiskMeta()), REQUEST, token=None).status, 401
        )

    def test_rejects_bad_input_without_calling_meta(self):
        meta = FakeRiskMeta()
        app = create_app(settings(), meta)
        for change in [
            {"as_of": ""},
            {"as_of": "Nov 2"},
            {"customers": []},
            {"customers": "lots"},
            {"top": 0},
            {"top": 99},
            {"ai": "yes"},
            {"customers": [dict(CUSTOMERS[0], overdue=[{"kind": "lunch", "due_date": AS_OF}])]},
            {"customers": [CUSTOMERS[0], CUSTOMERS[0]]},  # same ref twice
            {"customers": [dict(CUSTOMERS[0], salesperson_active="no")]},
        ]:
            r = post(app, dict(REQUEST, **change))
            self.assertEqual(r.status, 400, change)
        self.assertEqual(meta.calls, [])
