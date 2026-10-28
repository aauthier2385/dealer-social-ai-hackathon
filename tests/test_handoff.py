"""Feature 3 tests: salesperson handoff brief and warm intro. All Meta calls are faked."""

import json
import unittest

from app import handoff
from app.main import create_app
from tests.helpers import TEST_TOKEN, call, settings

FACTS = [
    {
        "id": "F1",
        "about": "Mark Stevenson",
        "category": "vehicle",
        "fact": "Bought 26 Ford Ranger Lariat",
    },
    {"id": "F2", "about": "Bell", "category": "pets", "fact": "Is a little Frenchie"},
    {"id": "F3", "about": "Bell", "category": "pets", "fact": "Eats french fries at lunch"},
    {"id": "F4", "about": "Mark Stevenson", "category": "work", "fact": "Is retired"},
    {
        "id": "F5",
        "about": "Mark Stevenson",
        "category": "personal",
        "fact": "From New York",
        "certainty": "uncertain",
    },
    {
        "id": "F6",
        "about": "Mark Stevenson",
        "category": "hobbies",
        "fact": "Cuts down a lot of trees",
    },
]
REQUEST = {
    "customer_name": "Mark Stevenson",
    "vehicle": "26 Ford Ranger Lariat",
    "from_salesperson": "Joey",
    "to_salesperson": "Andrew Smith",
    "store_name": "Demo Motors",
    "customer_since": "2026-10-27",
    "facts": FACTS,
}

GOOD_BRIEF = {
    "headline": "Retired Ranger Lariat owner who brings his Frenchie Bell everywhere.",
    "know_first": [
        {"point": "Bought a 26 Ranger Lariat for truck utility", "uses_facts": ["F1"]},
        {
            "point": "His little Frenchie Bell rides along and steals french fries",
            "uses_facts": ["F2", "F3"],
        },
        {"point": "He's retired", "uses_facts": ["F4"]},
        {"point": "May be from New York", "uses_facts": ["F5"]},
        {"point": "Loves fishing on weekends", "uses_facts": []},  # cites nothing: dropped
    ],
    "talk_about": [
        {"point": "Ask how the tree cutting is going", "uses_facts": ["F6"]},
        {"point": "Ask how Bell likes the new truck", "uses_facts": ["F2"]},
        {"point": "Ask about his 401k", "uses_facts": ["F4"]},  # money: dropped
    ],
    "intro_text": "Hey Mark, it's Andrew at Demo Motors. I'll be taking care of you from here on. "
    "Give Bell a french fry for me! Let me know if you need anything!",
    "intro_call_notes": ["Introduce yourself", "Ask how Bell is doing"],
    "intro_uses_facts": ["F2", "F3", "F6"],
}


class FakeBriefMeta:
    def __init__(self, brief=None):
        self.brief = GOOD_BRIEF if brief is None else brief
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        self.calls.append(json.loads(body))
        reply = {
            "model": "muse-spark-1.3",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(self.brief)}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 300, "total_tokens": 1100},
        }
        return 200, {}, json.dumps(reply).encode()


def post(app, body, token=TEST_TOKEN):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return call(app, "POST", "/v1/handoff/brief", headers=headers, body=json.dumps(body).encode())


def brief_with(**changes):
    brief = json.loads(json.dumps(GOOD_BRIEF))
    brief.update(changes)
    return brief


class BriefTests(unittest.TestCase):
    def run_brief(self, brief=None, request=None):
        r = post(create_app(settings(), FakeBriefMeta(brief)), request or REQUEST)
        self.assertEqual(r.status, 200, r.json())
        return r.json()

    def test_good_brief_keeps_grounded_points_only(self):
        body = self.run_brief()
        know = [p["point"] for p in body["know_first"]]
        self.assertNotIn("Loves fishing on weekends", know)  # invented: cited no fact
        self.assertEqual(len(know), 4)
        talk = [p["point"] for p in body["talk_about"]]
        self.assertNotIn("Ask about his 401k", talk)
        self.assertEqual(body["checks"]["points_dropped"], 2)
        self.assertEqual(body["checks"]["points_kept"], 6)
        self.assertEqual(body["to_salesperson"], "Andrew Smith")
        self.assertEqual(body["from_salesperson"], "Joey")

    def test_uncertain_points_are_marked(self):
        body = self.run_brief()
        flags = {p["point"]: p["not_sure"] for p in body["know_first"]}
        self.assertTrue(flags["May be from New York"])
        self.assertFalse(flags["He's retired"])

    def test_retired_customer_is_not_mistaken_for_a_departure(self):
        body = self.run_brief()
        self.assertIn("He's retired", [p["point"] for p in body["know_first"]])

    def test_intro_kept_and_tags_checked(self):
        intro = self.run_brief()["intro"]
        self.assertEqual(intro["safe_draft_used"], [])
        self.assertIn("Bell", intro["text"])
        # F6 (trees) was claimed but the intro never mentions it
        self.assertEqual([f["id"] for f in intro["uses_facts"]], ["F2", "F3"])

    def test_departure_reason_is_replaced(self):
        for bad in [
            "Hey Mark, it's Andrew at Demo Motors. Joey got fired so I'll be taking care of you!",
            "Hey Mark, it's Andrew at Demo Motors. Unfortunately Joey is no longer with us.",
            "Hey Mark, it's Andrew. Joey quit last week, but I've got you!",
        ]:
            intro = self.run_brief(brief_with(intro_text=bad))["intro"]
            self.assertEqual(intro["safe_draft_used"], ["text:departure_reason"], bad)
            self.assertIn("I'll be taking care of you from here on out", intro["text"])
            self.assertNotIn("Joey", intro["text"])

    def test_intro_must_name_both_people_and_stay_safe(self):
        cases = {
            "Hey there, I'll be taking care of you now at Demo Motors. Let me know!": "missing_new_salesperson",
            "Hey there, it's Andrew at Demo Motors, here to help!": "missing_customer_name",
            "Hey Mark, it's Andrew. Hope the hospital visit went ok!": "sensitive",
        }
        for text, problem in cases.items():
            intro = self.run_brief(brief_with(intro_text=text))["intro"]
            self.assertEqual(intro["safe_draft_used"], [f"text:{problem}"], text)

    def test_bad_headline_and_notes_fall_back(self):
        body = self.run_brief(
            brief_with(
                headline="Joey quit, and Mark still owes on his loan",
                intro_call_notes=["Explain Joey got fired", 7],
            )
        )
        self.assertEqual(body["headline"], "Mark Stevenson drives a 26 Ford Ranger Lariat.")
        self.assertNotIn("fired", json.dumps(body["intro"]))
        self.assertTrue(body["intro"]["call_notes"])

    def test_voice_samples_are_used_and_names_swapped(self):
        meta = FakeBriefMeta()
        app = create_app(settings(), meta)
        samples = ["Yo Kerrigan! Andrew here, hows the truck treating ya 👊"]
        body = post(app, dict(REQUEST, voice_samples=samples)).json()
        prompt = meta.calls[0]["messages"][0]["content"]
        self.assertIn("Yo {first_name}! Andrew here", prompt)
        self.assertNotIn("Kerrigan", prompt)
        self.assertEqual(body["voice"], {"source": "salesperson_samples", "samples_used": 1})

    def test_prompt_rules(self):
        meta = FakeBriefMeta()
        post(create_app(settings(), meta), REQUEST)
        request = meta.calls[0]
        self.assertEqual(request["response_format"]["json_schema"]["name"], "handoff_brief")
        self.assertEqual(request["reasoning_effort"], "low")
        system = request["messages"][0]["content"]
        self.assertIn("Never say or hint why the previous salesperson left", system)
        user = request["messages"][1]["content"]
        self.assertIn("Previous salesperson: Joey", user)
        self.assertIn("F5 [personal] (uncertain) about Mark Stevenson: From New York", user)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeBriefMeta()
        self.app = create_app(settings(), self.meta)

    def test_requires_token(self):
        self.assertEqual(post(self.app, REQUEST, token=None).status, 401)

    def test_rejects_bad_input_without_calling_meta(self):
        for change in [
            {"from_salesperson": ""},
            {"to_salesperson": "joey"},  # same person
            {"customer_name": ""},
            {"customer_since": "last year"},
            {"facts": "lots"},
            {"voice_samples": "text"},
        ]:
            r = post(self.app, dict(REQUEST, **change))
            self.assertEqual(r.status, 400, change)
        self.assertEqual(self.meta.calls, [])

    def test_sensitive_facts_never_reach_meta(self):
        facts = FACTS + [{"id": "F9", "fact": "Co-signed for his daughter"}]
        post(self.app, dict(REQUEST, facts=facts))
        self.assertNotIn("Co-signed", self.meta.calls[0]["messages"][1]["content"])

    def test_customer_since_is_optional(self):
        body = dict(REQUEST)
        del body["customer_since"]
        post(self.app, body)
        self.assertIn("Customer since: unknown", self.meta.calls[0]["messages"][1]["content"])

    def test_departure_words_list(self):
        self.assertTrue(handoff._DEPARTURE.search("he parted ways with the store"))
        self.assertFalse(handoff._DEPARTURE.search("how's retirement treating you"))
