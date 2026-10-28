"""Feature 2 tests: verified facts -> follow-up plan. All Meta calls are faked."""

import json
import unittest

from app import planner
from app.main import create_app
from tests.helpers import TEST_TOKEN, call, settings

FACTS = [
    {
        "id": "F1",
        "about": "Mike Carter",
        "category": "vehicle",
        "fact": "Bought a blue F-150",
        "timing": "",
        "certainty": "stated",
    },
    {
        "id": "F2",
        "about": "Emma",
        "category": "life_event",
        "fact": "Daughter Emma turns 16",
        "timing": "March",
        "certainty": "stated",
    },
    {
        "id": "F3",
        "about": "Mike Carter",
        "category": "pets",
        "fact": "Dog Duke rides in the truck bed",
        "timing": "",
        "certainty": "stated",
    },
    {
        "id": "F4",
        "about": "Mike Carter",
        "category": "contact_preference",
        "fact": "Prefers texts over calls",
        "timing": "",
        "certainty": "stated",
    },
    {
        "id": "F5",
        "about": "Mike Carter",
        "category": "personal",
        "fact": "Brother-in-law may be shopping for a truck",
        "timing": "spring",
        "certainty": "uncertain",
    },
]
REQUEST = {
    "customer_name": "Mike Carter",
    "vehicle": "blue F-150",
    "salesperson_name": "Andrew Smith",
    "store_name": "Demo Motors Ford",
    "delivery_date": "2026-10-27",
    "birthday": "06-14",
    "facts": FACTS,
}


def slot(name, text="", notes=None, card="", uses=None, why="Personal touch."):
    return {
        "slot": name,
        "text": text,
        "call_notes": notes or [],
        "card": card,
        "uses_facts": uses or [],
        "why": why,
    }


GOOD_PLAN = {
    "slots": [
        slot(
            "day1",
            "Hey Mike, it's Andrew at Demo Motors Ford. How's the new F-150 treating you and Duke? Let me know if you need anything!",
            uses=["F1", "F3"],
        ),
        slot(
            "card",
            card="Mike, thank you for choosing us! Enjoy the F-150, and give Duke a scratch for me. - Andrew",
            uses=["F1", "F3"],
        ),
        slot(
            "day30",
            notes=["Ask how the F-150 has been", "Ask about Emma getting ready to drive"],
            uses=["F2"],
        ),
        slot(
            "day90",
            "Hey Mike, it's Andrew at Demo Motors Ford. Your F-150 is due for its first service. Grab a time here: {booking_link}",
            uses=["F1"],
        ),
        slot(
            "birthday",
            "Hey Mike, happy birthday 🎂 🥳 🎉!! Enjoy your day!",
            notes=["Say happy birthday"],
        ),
        slot(
            "anniversary",
            "Hey Mike, can't believe it's been a year with the F-150!! Holler at me if you need anything!",
            notes=["Ask about the year"],
            uses=["F1"],
        ),
    ],
    "life_events": [
        {
            "fact_id": "F2",
            "date": "2027-03-01",
            "title": "Emma turns 16",
            "channel": "text",
            "text": "Hey Mike, isn't Emma turning 16 this month? Wishing her luck on the driving test!",
            "call_notes": [],
            "why": "Emma's milestone.",
        },
    ],
}


class FakePlanMeta:
    def __init__(self, plan=None):
        self.plan = GOOD_PLAN if plan is None else plan
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        self.calls.append(json.loads(body))
        reply = {
            "model": "muse-spark-1.3",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(self.plan)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300},
        }
        return 200, {}, json.dumps(reply).encode()


def post_plan(app, body, token=TEST_TOKEN):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return call(
        app, "POST", "/v1/plan/touchpoints", headers=headers, body=json.dumps(body).encode()
    )


def plan_with(**slot_overrides):
    plan = json.loads(json.dumps(GOOD_PLAN))
    for s in plan["slots"]:
        s.update(slot_overrides.get(s["slot"], {}))
    return plan


class ScheduleTests(unittest.TestCase):
    def test_cadence_dates_come_from_code(self):
        body = post_plan(create_app(settings(), FakePlanMeta()), REQUEST).json()
        by_kind = {t["kind"]: t for t in body["touchpoints"]}
        self.assertEqual(by_kind["day1"]["date"], "2026-10-28")
        self.assertEqual(by_kind["card"]["date"], "2026-10-29")
        self.assertEqual(by_kind["day30"]["date"], "2026-11-26")
        self.assertEqual(by_kind["day90"]["date"], "2027-01-25")
        self.assertEqual(by_kind["birthday"]["date"], "2027-06-14")
        self.assertEqual(by_kind["anniversary"]["date"], "2027-10-27")
        self.assertEqual(by_kind["life_event"]["date"], "2027-03-01")
        dates = [t["date"] for t in body["touchpoints"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual([t["id"] for t in body["touchpoints"]][:2], ["T1", "T2"])

    def test_no_birthday_slot_unless_birthday_is_known(self):
        request = dict(REQUEST, birthday="")
        body = post_plan(create_app(settings(), FakePlanMeta()), request).json()
        self.assertNotIn("birthday", [t["kind"] for t in body["touchpoints"]])

    def test_leap_day_birthday_and_anniversary(self):
        p = planner.parse_input(dict(REQUEST, delivery_date="2028-02-29", birthday="02-29"))
        dates = {s["slot"]: s["date"].isoformat() for s in planner.schedule(p)}
        self.assertEqual(dates["anniversary"], "2029-02-28")
        self.assertEqual(dates["birthday"], "2029-02-28")

    def test_contact_preference_sets_the_day_after_channel(self):
        cases = {
            "Prefers texts over calls": "text",
            "Text him, don't call": "text",
            "Prefers calls over texts": "call",
            "Usually available to call": "call",
            "Responds to text pretty quick": "text",
        }
        for said, expected in cases.items():
            facts = [planner.Fact("F1", "", "contact_preference", said, "", "stated")]
            self.assertEqual(planner.contact_channel(facts), expected, said)
        both = [
            planner.Fact(
                "F1", "", "contact_preference", "Responds to text pretty quick", "", "stated"
            ),
            planner.Fact("F2", "", "contact_preference", "Usually available to call", "", "stated"),
        ]
        self.assertEqual(planner.contact_channel(both), "call_or_text")
        self.assertEqual(planner.contact_channel([]), "call_or_text")


class DraftCheckTests(unittest.TestCase):
    def run_plan(self, plan):
        return post_plan(create_app(settings(), FakePlanMeta(plan)), REQUEST).json()

    def test_good_drafts_are_kept_with_their_facts(self):
        body = self.run_plan(GOOD_PLAN)
        day1 = body["touchpoints"][0]
        self.assertEqual(day1["channel"], "text")  # F4: prefers texts
        self.assertIn("Duke", day1["text"])
        self.assertEqual([f["id"] for f in day1["uses_facts"]], ["F1", "F3"])
        self.assertEqual(day1["safe_draft_used"], [])
        self.assertEqual(body["checks"]["replaced_with_safe_draft"], 0)
        self.assertEqual(body["checks"]["life_events_kept"], 1)

    def test_business_on_a_birthday_is_replaced(self):
        plan = plan_with(birthday={"text": "Happy birthday Mike! Come in for a service special 🎂"})
        tp = {t["kind"]: t for t in self.run_plan(plan)["touchpoints"]}["birthday"]
        self.assertEqual(tp["safe_draft_used"], ["text:business_on_birthday"])
        self.assertNotIn("service", tp["text"].lower())
        self.assertIn("happy birthday", tp["text"].lower())

    def test_sensitive_draft_is_replaced(self):
        plan = plan_with(day1={"text": "Hey Mike, hope your wife is out of the hospital now!"})
        tp = self.run_plan(plan)["touchpoints"][0]
        self.assertEqual(tp["safe_draft_used"], ["text:sensitive"])
        self.assertNotIn("hospital", json.dumps(tp))
        self.assertEqual(tp["uses_facts"], [])

    def test_too_long_and_stray_placeholder_are_replaced(self):
        plan = plan_with(
            day1={"text": "Hey Mike! " + "So glad you love it. " * 30},
            anniversary={"text": "Hey Mike, it's been a year! Book here {booking_link}"},
        )
        tps = {t["kind"]: t for t in self.run_plan(plan)["touchpoints"]}
        self.assertEqual(tps["day1"]["safe_draft_used"], ["text:too_long"])
        self.assertEqual(tps["anniversary"]["safe_draft_used"], ["text:placeholder"])

    def test_service_text_always_carries_the_booking_link(self):
        plan = plan_with(day90={"text": "Hey Mike, time for your first service visit!"})
        tp = {t["kind"]: t for t in self.run_plan(plan)["touchpoints"]}["day90"]
        self.assertTrue(tp["text"].endswith("{booking_link}"))
        self.assertEqual(tp["safe_draft_used"], [])

    def test_unknown_fact_ids_are_dropped(self):
        plan = plan_with(day1={"uses_facts": ["F1", "F99", "F1"]})
        tp = self.run_plan(plan)["touchpoints"][0]
        self.assertEqual([f["id"] for f in tp["uses_facts"]], ["F1"])

    def test_missing_slots_get_safe_drafts_in_the_salespersons_voice(self):
        body = self.run_plan({"slots": [], "life_events": []})
        tps = {t["kind"]: t for t in body["touchpoints"]}
        self.assertEqual(body["checks"]["replaced_with_safe_draft"], 6)
        self.assertTrue(
            tps["day1"]["text"].startswith("Hey Mike, it's Andrew at Demo Motors Ford.")
        )
        self.assertIn("{booking_link}", tps["day90"]["text"])
        self.assertTrue(tps["card"]["card"].endswith("- Andrew"))
        self.assertTrue(tps["day30"]["call_notes"])

    def test_bad_call_notes_are_filtered(self):
        plan = plan_with(
            day30={"call_notes": ["Ask about Emma", "Ask about his credit score", 5, ""]}
        )
        tp = {t["kind"]: t for t in self.run_plan(plan)["touchpoints"]}["day30"]
        self.assertEqual(tp["call_notes"], ["Ask about Emma"])

    def test_life_events_are_checked(self):
        bad_events = [
            # uncertain fact
            {
                "fact_id": "F5",
                "date": "2027-04-01",
                "title": "Brother-in-law truck",
                "channel": "call",
                "text": "",
                "call_notes": ["Ask about it"],
                "why": "",
            },
            # fact without timing
            {
                "fact_id": "F3",
                "date": "2027-04-01",
                "title": "Duke",
                "channel": "call",
                "text": "",
                "call_notes": ["Ask about Duke"],
                "why": "",
            },
            # date before delivery
            {
                "fact_id": "F2",
                "date": "2026-01-01",
                "title": "Emma",
                "channel": "call",
                "text": "",
                "call_notes": ["Ask"],
                "why": "",
            },
            # more than two years out
            {
                "fact_id": "F2",
                "date": "2029-03-01",
                "title": "Emma",
                "channel": "call",
                "text": "",
                "call_notes": ["Ask"],
                "why": "",
            },
            # unknown fact
            {
                "fact_id": "F42",
                "date": "2027-03-01",
                "title": "x",
                "channel": "call",
                "text": "",
                "call_notes": ["Ask"],
                "why": "",
            },
        ]
        plan = dict(GOOD_PLAN, life_events=bad_events)
        body = self.run_plan(plan)
        self.assertEqual(body["checks"]["life_events_kept"], 0)
        self.assertEqual(body["checks"]["life_events_dropped"], 5)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakePlanMeta()
        self.app = create_app(settings(), self.meta)

    def test_requires_token(self):
        self.assertEqual(post_plan(self.app, REQUEST, token=None).status, 401)

    def test_rejects_bad_input_without_calling_meta(self):
        for change in [
            {"delivery_date": "10/27/2026"},
            {"delivery_date": ""},
            {"birthday": "June 14"},
            {"birthday": "02-30"},
            {"customer_name": ""},
            {"salesperson_name": 7},
            {"facts": "lots"},
            {"facts": [{"id": "F1"}]},
        ]:
            r = post_plan(self.app, dict(REQUEST, **change))
            self.assertEqual(r.status, 400, change)
        self.assertEqual(self.meta.calls, [])

    def test_sensitive_facts_sent_by_a_caller_never_reach_meta(self):
        facts = FACTS + [
            {
                "id": "F9",
                "category": "personal",
                "fact": "Wife was in the hospital",
                "timing": "",
                "certainty": "stated",
            }
        ]
        post_plan(self.app, dict(REQUEST, facts=facts))
        prompt = self.meta.calls[0]["messages"][1]["content"]
        self.assertNotIn("hospital", prompt)
        self.assertIn("F2 [life_event] (stated, March)", prompt)

    def test_request_uses_structured_output_and_low_reasoning(self):
        post_plan(self.app, REQUEST)
        request = self.meta.calls[0]
        self.assertEqual(request["response_format"]["json_schema"]["name"], "touchpoint_plan")
        self.assertEqual(request["reasoning_effort"], "low")
        slots = request["response_format"]["json_schema"]["schema"]["properties"]["slots"]
        self.assertEqual(
            slots["items"]["properties"]["slot"]["enum"],
            ["day1", "card", "day30", "day90", "birthday", "anniversary"],
        )

    def test_favicon(self):
        r = call(self.app, "GET", "/favicon.ico")
        self.assertEqual(r.status, 200)
        self.assertIn("svg", r.headers["content-type"])


if __name__ == "__main__":
    unittest.main()


class SameIndividualTests(unittest.TestCase):
    def test_prompt_treats_a_pet_name_and_breed_as_one_animal(self):
        self.assertIn('same "about" describe the same individual', planner.PLAN_SYSTEM)


JOEY_SAMPLES = [
    "Yo Kerrigan! Joey here w Demo Motors, hows the new ride treating ya 👊",
    "Hey Gary, Joey here. that Mustang still pulling good? hit me up if u need anything",
    "Happy birthday Tina!!! 🎉🎉 have a good one",
    "ok",  # too short to show a style: dropped
    "Hey Sam, your payment is due Friday",  # sensitive: dropped
]


class VoiceSampleTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakePlanMeta()
        self.app = create_app(settings(), self.meta)

    def system_prompt(self, body):
        post_plan(self.app, body)
        return self.meta.calls[-1]["messages"][0]["content"]

    def test_default_voice_without_samples(self):
        prompt = self.system_prompt(REQUEST)
        self.assertEqual(prompt, planner.PLAN_SYSTEM)
        self.assertIn("Hey Gary, just wanted to check in", prompt)

    def test_samples_replace_the_default_voice_with_names_swapped_out(self):
        prompt = self.system_prompt(dict(REQUEST, voice_samples=JOEY_SAMPLES))
        self.assertIn("Yo {first_name}! Joey here w Demo Motors", prompt)
        self.assertIn("Hey {first_name}, Joey here. that Mustang", prompt)
        self.assertIn("Happy birthday {first_name}!!!", prompt)
        for leaked in ("Kerrigan", "Gary", "Tina", "due Friday", "Hey John, it's Andrew"):
            self.assertNotIn(leaked, prompt)
        self.assertIn("ONLY for style", prompt)
        self.assertIn("Slots:", prompt)  # the cadence rules still apply

    def test_response_says_which_voice_was_used(self):
        body = post_plan(self.app, dict(REQUEST, voice_samples=JOEY_SAMPLES)).json()
        self.assertEqual(body["voice"], {"source": "salesperson_samples", "samples_used": 3})
        body = post_plan(self.app, REQUEST).json()
        self.assertEqual(body["voice"], {"source": "store_default", "samples_used": 0})

    def test_draft_that_borrows_a_sample_customer_is_replaced(self):
        plan = plan_with(day1={"text": "Yo Gary! Joey here, hows the F-150 treating ya 👊"})
        app = create_app(settings(), FakePlanMeta(plan))
        body = post_plan(app, dict(REQUEST, voice_samples=JOEY_SAMPLES)).json()
        day1 = body["touchpoints"][0]
        self.assertEqual(day1["safe_draft_used"], ["text:name_from_voice_sample"])
        self.assertNotIn("Gary", day1["text"])

    def test_same_first_name_as_a_sample_is_fine(self):
        samples = ["Hey Mike, Joey here. how's the truck?"]
        body = post_plan(
            create_app(settings(), FakePlanMeta()), dict(REQUEST, voice_samples=samples)
        ).json()
        self.assertEqual(body["checks"]["replaced_with_safe_draft"], 0)

    def test_bad_samples_are_rejected(self):
        for bad in ["text", ["a"] * 6, [7]]:
            r = post_plan(self.app, dict(REQUEST, voice_samples=bad))
            self.assertEqual(r.status, 400, bad)

    def test_greeting_names_only(self):
        self.assertEqual(planner.anonymize_sample("Hey there, Joey here")[1], "")
        self.assertEqual(
            planner.anonymize_sample("Good morning Kerrigan! How's Kerrigan's truck?"),
            ("Good morning {first_name}! How's {first_name}'s truck?", "Kerrigan"),
        )


class PronounRuleTests(unittest.TestCase):
    def test_extraction_never_turns_a_pronoun_into_a_fact(self):
        from app import memory

        self.assertIn('not \\\n"Is female"'.replace("\\\n", ""), memory.EXTRACT_SYSTEM)


class VoiceVarietyTests(unittest.TestCase):
    def test_custom_voice_limits_repeated_signature_phrases(self):
        voice = planner.custom_voice(["How's it hanging, my dude!! Joey here"])
        self.assertIn("at most two messages", voice)
        self.assertIn("never paste a whole sample sentence", voice)
        self.assertIn("write it as a card, not a text", voice)


class UsesFactsTests(unittest.TestCase):
    """Real run: a day-after text about the new Ranger was tagged with "Previously owned a 2010 Ranger"."""

    def plan(self):
        return planner.parse_input(
            {
                "customer_name": "Mark Stevenson",
                "vehicle": "26 Ford Ranger Lariat",
                "salesperson_name": "Joey",
                "delivery_date": "2026-10-27",
                "facts": [
                    {"id": "F1", "about": "Mark Stevenson", "fact": "Bought 26 Ford Ranger Lariat"},
                    {"id": "F5", "about": "Bill", "fact": "Is a little Frenchie"},
                    {"id": "F8", "about": "Mark Stevenson", "fact": "Cuts down a lot of trees"},
                    {
                        "id": "F10",
                        "about": "Mark Stevenson",
                        "fact": "Previously owned a 2010 Ranger",
                    },
                ],
            }
        )

    def ids(self, content, ids):
        refs = [{"id": i, "fact": ""} for i in ids]
        return [r["id"] for r in planner.mentioned_facts(refs, content, self.plan())]

    def test_unmentioned_fact_is_untagged(self):
        text = "Hey Mark, it's Joey at Demo Motors. How's the new Ford Ranger Lariat? Ask how Bill is doing"
        self.assertEqual(self.ids(text, ["F1", "F5", "F10"]), ["F1", "F5"])

    def test_mentioned_facts_stay(self):
        notes = "Ask about the old 2010 Ranger. Ask how the tree cutting is going"
        self.assertEqual(self.ids(notes, ["F8", "F10"]), ["F8", "F10"])

    def test_pet_counts_by_name_or_breed(self):
        self.assertEqual(self.ids("Give your Frenchie a treat", ["F5"]), ["F5"])
        self.assertEqual(self.ids("Tell Bill hi", ["F5"]), ["F5"])
