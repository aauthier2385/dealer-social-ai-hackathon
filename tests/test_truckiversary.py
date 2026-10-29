"""Feature 6 tests: Truckiversary posts. Muse Image, SAM 3.1 and Muse Spark are faked."""

import base64
import io
import json
import unittest

from PIL import Image, ImageStat

from app import truckiversary
from app.main import create_app
from tests.helpers import TEST_TOKEN, call, settings
from tests.test_photo import BYSTANDER, CUSTOMER, FakeSam, make_photo

GOOD_CAPTION = "Hey Mark!! Happy 1st Truckiversary with the Ranger 🎉 Hope she's still treating you right! - Andrew"


def scene_png(color=(250, 180, 60)):
    img = Image.new("RGB", (1024, 1280), color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class FakeMeta:
    """Routes by request: Muse Image scenes, SAM segmentation, Muse Spark captions."""

    def __init__(self, caption=GOOD_CAPTION, image=None, image_status=200, refuse_image=False):
        self.caption = caption
        self.image = scene_png() if image is None else image
        self.image_status = image_status
        self.refuse_image = refuse_image
        self.sam = FakeSam()
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        request = json.loads(body)
        self.calls.append({"url": url, "json": request})
        if url.endswith("/responses") and request.get("model") == "muse-image-1.0":
            if self.image_status != 200:
                return self.image_status, {}, b"{}"
            if self.refuse_image:
                out = [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
            else:
                out = [
                    {"type": "reasoning", "id": "rs_1"},
                    {
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": base64.b64encode(self.image).decode(),
                    },
                ]
            payload = {
                "status": "completed",
                "model": "muse-image-1.0",
                "output": out,
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 600,
                    "total_tokens": 1500,
                    "output_tokens_details": {"reasoning_tokens": 100},
                },
            }
            return 200, {}, json.dumps(payload).encode()
        if url.endswith("/responses"):
            return self.sam(url, headers, body, timeout, max_bytes)
        reply = {
            "model": "muse-spark-1.3",
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps({"caption": self.caption})}}
            ],
            "usage": {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
        }
        return 200, {}, json.dumps(reply).encode()


def request(**changes):
    body = {
        "photo": "data:image/jpeg;base64," + base64.b64encode(make_photo()).decode(),
        "customer_first_name": "Mark",
        "vehicle": "Ford Ranger Lariat",
        "vehicle_color": "carbonized gray",
        "store_name": "Demo Motors",
        "salesperson_name": "Andrew Authier",
        "years": 1,
        "customer_consent": True,
    }
    body.update(changes)
    return body


def post(app, body, token=TEST_TOKEN):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return call(app, "POST", "/v1/truckiversary", headers=headers, body=json.dumps(body).encode())


def card_image(body):
    return Image.open(io.BytesIO(base64.b64decode(body["image"].split(",", 1)[1])))


class CardTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeMeta()
        self.app = create_app(settings(), self.meta)

    def test_card_and_caption(self):
        r = post(self.app, request())
        self.assertEqual(r.status, 200, r.body[:200])
        body = r.json()
        self.assertEqual(body["headline"], "Happy 1st Truckiversary, Mark!")
        self.assertEqual(body["caption"], GOOD_CAPTION)
        img = card_image(body)
        self.assertEqual(img.size, (1080, 1350))
        self.assertEqual(len(img.getexif()), 0)
        c = body["checks"]
        self.assertTrue(c["photo_untouched_by_ai"])
        self.assertEqual((c["faces_blurred"], c["plates_blurred"]), (1, 1))
        self.assertFalse(c["caption_replaced_with_safe_draft"])
        self.assertEqual(body["models"], {"image": "muse-image-1.0", "caption": "muse-spark-1.3"})
        self.assertEqual(body["usage"]["reasoning_tokens"], 100)

    def test_scene_is_the_background_and_real_photo_sits_on_top(self):
        body = post(self.app, request(blur_bystanders=False)).json()
        img = card_image(body).convert("RGB")
        # A corner shows the generated scene (orange), the middle shows the striped real photo.
        corner = ImageStat.Stat(img.crop((5, 300, 60, 400))).mean
        self.assertGreater(corner[0], 200)
        self.assertLess(corner[2], 120)
        middle = ImageStat.Stat(img.crop((400, 500, 680, 800)).convert("L")).stddev[0]
        self.assertGreater(middle, 20)  # the stripes of the real photo, not a flat scene

    def test_prompt_forbids_people_text_and_logos(self):
        post(self.app, request())
        image_calls = [c for c in self.meta.calls if c["json"].get("model") == "muse-image-1.0"]
        self.assertEqual(len(image_calls), 1)
        prompt = image_calls[0]["json"]["input"]
        self.assertIn("carbonized gray Ford Ranger Lariat", prompt)
        for rule in ("no people", "no faces", "no text", "no logos", "no license plates"):
            self.assertIn(rule, prompt)
        self.assertIs(image_calls[0]["json"]["store"], False)
        # The customer's photo is never sent to the image model.
        self.assertNotIn("input_image", json.dumps(image_calls[0]["json"]))

    def test_bystanders_blurred_before_the_card_unless_turned_off(self):
        post(self.app, request())
        sam = [c for c in self.meta.calls if c["json"].get("model") == "sam-3.1"]
        self.assertEqual(len(sam), 2)
        meta = FakeMeta()
        post(create_app(settings(), meta), request(blur_bystanders=False))
        self.assertEqual([c for c in meta.calls if c["json"].get("model") == "sam-3.1"], [])

    def test_headline_for_cars_and_later_years(self):
        body = post(self.app, request(vehicle="Ford Escape Active", years=2)).json()
        self.assertEqual(body["headline"], "Happy 2nd Anniversary, Mark!")
        body = post(self.app, request(vehicle="Ram 1500", years=5)).json()
        self.assertEqual(body["headline"], "Happy 5th Truckiversary, Mark!")

    def test_caption_uses_the_salesperson_voice_samples(self):
        samples = ["Yo Kerrigan! Joey here w Demo Motors, hows the ride treating ya 👊"]
        body = post(self.app, request(voice_samples=samples)).json()
        system = [c for c in self.meta.calls if c["url"].endswith("/chat/completions")][0]["json"][
            "messages"
        ][0]["content"]
        self.assertIn("Yo {first_name}! Joey here", system)
        self.assertNotIn("Kerrigan", system)
        self.assertIn("Never mention anything personal", system)
        self.assertEqual(body["voice"]["samples_used"], 1)


class CaptionCheckTests(unittest.TestCase):
    def run_caption(self, caption, **changes):
        body = post(create_app(settings(), FakeMeta(caption=caption)), request(**changes)).json()
        return body["caption"], body["checks"]["caption_problem"]

    def test_bad_captions_are_replaced(self):
        cases = {
            "": "empty",
            "Happy Truckiversary Mark! " + "🎉" * 300: "too_long",
            "Happy Truckiversary Mark! Come see our trade-in specials!": "sales_pitch",
            "Happy Truckiversary Mark! Hope your wife's surgery went well": "sensitive",
            "Happy Truckiversary buddy!": "missing_name",
        }
        for caption, problem in cases.items():
            text, found = self.run_caption(caption)
            self.assertEqual(found, problem, caption)
            self.assertTrue(text.startswith("Happy 1st Truckiversary, Mark!"), text)

    def test_caption_cannot_borrow_a_sample_customer(self):
        samples = ["Yo Kerrigan! Joey here, hows the truck treating ya 👊"]
        text, problem = self.run_caption(
            "Yo Kerrigan and Mark! Happy Truckiversary!", voice_samples=samples
        )
        self.assertEqual(problem, "name_from_voice_sample")


class FailureTests(unittest.TestCase):
    def test_image_failure_returns_no_card(self):
        r = post(create_app(settings(), FakeMeta(image_status=500)), request())
        self.assertEqual(r.status, 503)
        self.assertNotIn("image", r.json())

    def test_refused_or_unreadable_image(self):
        self.assertEqual(
            post(create_app(settings(), FakeMeta(refuse_image=True)), request()).status, 502
        )
        self.assertEqual(
            post(create_app(settings(), FakeMeta(image=b"not an image")), request()).status, 502
        )


class InputTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeMeta()
        self.app = create_app(settings(), self.meta)

    def test_requires_token(self):
        self.assertEqual(post(self.app, request(), token=None).status, 401)

    def test_consent_is_required(self):
        for consent in (None, False, "yes"):
            r = post(self.app, request(customer_consent=consent))
            self.assertEqual(r.status, 400)
            self.assertIn("customer's OK", r.json()["message"])
        self.assertEqual(self.meta.calls, [])

    def test_bad_input_is_rejected_without_calling_meta(self):
        for change in [
            {"photo": ""},
            {"photo": "data:image/jpeg;base64,!!!"},
            {"photo": "data:image/jpeg;base64," + base64.b64encode(b"not a photo").decode()},
            {"customer_first_name": "<b>Mark</b>"},
            {"customer_first_name": ""},
            {"vehicle": ""},
            {"vehicle": "Ranger {booking_link}"},
            {"years": 0},
            {"years": "one"},
            {"blur_bystanders": "no"},
        ]:
            r = post(self.app, request(**change))
            self.assertEqual(r.status, 400, change)
        self.assertEqual(self.meta.calls, [])

    def test_truck_detection(self):
        card = truckiversary.CardInput(
            b"", "Mark", "Chevy Silverado 1500", "", "S", "A", 1, False, [], frozenset()
        )
        self.assertTrue(card.is_truck)
        card.vehicle = "Ford Bronco Sport"
        self.assertFalse(card.is_truck)


# Unused-import guard for helpers the tests share with test_photo.
_ = (BYSTANDER, CUSTOMER)


class ImageFormatTests(unittest.TestCase):
    """The image can arrive in slightly different shapes; all of them should work."""

    def run_with(self, output):
        class Meta(FakeMeta):
            def __call__(self, url, headers, body, timeout, max_bytes):
                request = json.loads(body)
                if url.endswith("/responses") and request.get("model") == "muse-image-1.0":
                    payload = {
                        "status": "completed",
                        "model": "muse-image-1.0",
                        "output": output,
                        "usage": {},
                    }
                    return 200, {}, json.dumps(payload).encode()
                return super().__call__(url, headers, body, timeout, max_bytes)

        return post(create_app(settings(), Meta()), request(blur_bystanders=False))

    def test_wrapped_base64_and_data_urls(self):
        b64 = base64.b64encode(scene_png()).decode()
        wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
        for output in [
            [{"type": "image_generation_call", "result": wrapped}],
            [{"type": "image_generation_call", "result": "data:image/png;base64," + b64}],
            [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_image", "image_url": "data:image/png;base64," + b64}
                    ],
                }
            ],
            [{"type": "message", "content": [{"type": "output_image", "b64_json": b64}]}],
        ]:
            self.assertEqual(self.run_with(output).status, 200, str(output)[:80])

    def test_missing_image_is_logged_with_a_reason(self):
        with self.assertLogs("dsai", level="WARNING") as logs:
            r = self.run_with([{"type": "reasoning"}, {"type": "message", "content": []}])
        self.assertEqual(r.status, 502)
        self.assertTrue(
            any("no image in the response" in line and "reasoning" in line for line in logs.output)
        )
