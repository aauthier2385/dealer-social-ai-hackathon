"""Feature 5 tests: photo privacy blur with SAM 3.1. Meta is faked with real-format records."""

import base64
import io
import json
import unittest

from meta_sam_parser._mask_codec import _encode_raster
from PIL import Image, ImageDraw

from app import photo
from app.config import ConfigError
from app.main import create_app
from tests.helpers import TEST_TOKEN, call, settings

W, H = 800, 600
# Two people posing (large faces), one person in the background (small face), one plate.
CUSTOMER = (300, 120, 419, 269)  # inclusive boxes, as SAM sends them
SPOUSE = (460, 150, 559, 279)
BYSTANDER = (40, 60, 69, 99)
PLATE = (600, 470, 719, 509)


def sam_text(boxes, width=W, height=H, oval=True):
    """Build SAM API output text in the real record grammar, with real one_bit masks."""
    records = []
    for n, (x1, y1, x2, y2) in enumerate(boxes):
        mw, mh = x2 - x1 + 1, y2 - y1 + 1
        mask = Image.new("L", (mw, mh), 0)
        draw = ImageDraw.Draw(mask)
        (draw.ellipse if oval else draw.rectangle)((0, 0, mw - 1, mh - 1), fill=1)
        payload = _encode_raster(list(mask.tobytes()), mw, mh)
        records.append(
            f"{n}<|box;x1={x1};y1={y1};x2={x2};y2={y2};w={width};h={height};c=0.93|>"
            f"<|mask;x=0;y=0;data={mh},{mw},{payload}|>"
        )
    return "<0f>" + ",".join(records) if records else ""


def make_photo(fmt="JPEG", exif=None, size=(W, H)):
    """A busy, textured scene so blurring visibly changes pixels."""
    img = Image.new("RGB", size, (90, 120, 160))
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], 8):
        d.line((x, 0, x, size[1]), fill=(240, 240, 240) if (x // 8) % 2 else (20, 20, 20))
    out = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(out, format=fmt, quality=95, **kwargs)
    return out.getvalue()


class FakeSam:
    def __init__(self, faces=None, plates=None, status=200, faces_text=None, refuse=False):
        self.faces = [CUSTOMER, SPOUSE, BYSTANDER] if faces is None else faces
        self.plates = [PLATE] if plates is None else plates
        self.status = status
        self.faces_text = faces_text
        self.refuse = refuse
        self.calls = []

    def __call__(self, url, headers, body, timeout, max_bytes):
        request = json.loads(body)
        self.calls.append({"url": url, "json": request, "max_bytes": max_bytes})
        content = request["input"][0]["content"]
        concept = content[0]["text"]
        if self.status != 200 and concept == "license plate":
            return self.status, {}, b"{}"
        if self.refuse:
            output = [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
            return 200, {}, json.dumps({"status": "completed", "output": output}).encode()
        if concept == "face":
            text = self.faces_text if self.faces_text is not None else sam_text(self.faces)
        else:
            text = sam_text(self.plates)
        payload = {
            "status": "completed",
            "model": "sam-3.1",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050},
        }
        return 200, {}, json.dumps(payload).encode()


def post(app, data, query="", content_type="image/jpeg", token=TEST_TOKEN):
    headers = {"content-type": content_type}
    if token:
        headers["authorization"] = f"Bearer {token}"
    path = "/v1/photo/privacy-blur" + (f"?{query}" if query else "")
    return call(app, "POST", path, headers=headers, body=data)


def decode(body):
    data = base64.b64decode(body["image"].split(",", 1)[1])
    return Image.open(io.BytesIO(data))


def center(box):
    x1, y1, x2, y2 = box
    return (x1 + x2) // 2, (y1 + y2) // 2


def detail(image, box):
    """Standard deviation of brightness inside the box: blurring destroys detail."""
    from PIL import ImageStat

    x1, y1, x2, y2 = box
    return ImageStat.Stat(image.crop((x1, y1, x2 + 1, y2 + 1)).convert("L")).stddev[0]


def changed(original, result, box):
    """How much detail is left inside the box after processing (1.0 = untouched)."""
    return detail(result, box) / max(detail(original, box), 1e-6)


class BlurTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeSam()
        self.app = create_app(settings(), self.meta)
        self.source = make_photo()
        self.original = Image.open(io.BytesIO(self.source)).convert("RGB")

    def test_main_subjects_kept_background_and_plate_blurred(self):
        body = post(self.app, self.source).json()
        decisions = {f["id"]: (f["decision"], f["reason"]) for f in body["faces"]}
        self.assertEqual(decisions["F1"], ("kept", "main subject"))
        self.assertEqual(decisions["F2"], ("kept", "main subject"))
        self.assertEqual(decisions["F3"], ("blurred", "background person"))
        self.assertEqual(body["plates"][0]["decision"], "blurred")
        c = body["checks"]
        self.assertEqual(
            (c["faces_found"], c["faces_kept"], c["faces_blurred"], c["plates_blurred"]),
            (3, 2, 1, 1),
        )

    def test_pixels_really_change_only_where_blurred(self):
        result = decode(post(self.app, self.source).json()).convert("RGB")
        self.assertEqual(result.size, (W, H))
        # The inner part of each blurred object loses almost all of its detail...
        inner = lambda b: (b[0] + 6, b[1] + 6, b[2] - 6, b[3] - 6)  # noqa: E731
        self.assertLess(changed(self.original, result, inner(BYSTANDER)), 0.35)
        self.assertLess(changed(self.original, result, inner(PLATE)), 0.35)
        # ...while kept faces and the rest of the photo keep theirs.
        self.assertGreater(changed(self.original, result, CUSTOMER), 0.9)
        self.assertGreater(changed(self.original, result, (150, 400, 250, 450)), 0.9)

    def test_tap_to_keep_or_blur_overrides(self):
        kx, ky = center(BYSTANDER)
        bx, by = center(SPOUSE)
        body = post(self.app, self.source, f"keep={kx},{ky}&blur={bx},{by}").json()
        decisions = {f["id"]: (f["decision"], f["reason"]) for f in body["faces"]}
        self.assertEqual(decisions["F3"], ("kept", "you chose to keep"))
        self.assertEqual(decisions["F2"], ("blurred", "you chose to blur"))
        self.assertEqual(decisions["F1"], ("kept", "main subject"))

    def test_blur_all_faces(self):
        body = post(self.app, self.source, "blur_all_faces=1").json()
        self.assertEqual({f["decision"] for f in body["faces"]}, {"blurred"})

    def test_plates_cannot_be_kept(self):
        px, py = center(PLATE)
        body = post(self.app, self.source, f"keep={px},{py}").json()
        self.assertEqual(body["plates"][0]["decision"], "blurred")

    def test_no_faces_or_plates_is_fine(self):
        app = create_app(settings(), FakeSam(faces=[], plates=[]))
        body = post(app, self.source).json()
        self.assertEqual(body["faces"], [])
        self.assertEqual(body["checks"]["faces_found"], 0)

    def test_request_to_sam(self):
        post(self.app, self.source)
        self.assertEqual(len(self.meta.calls), 2)
        concepts = sorted(c["json"]["input"][0]["content"][0]["text"] for c in self.meta.calls)
        self.assertEqual(concepts, ["face", "license plate"])
        first = self.meta.calls[0]
        self.assertTrue(first["url"].endswith("/v1/responses"))
        self.assertEqual(first["json"]["model"], "sam-3.1")
        self.assertIs(first["json"]["store"], False)
        self.assertEqual(first["json"]["metadata"], {"mask_encoding": "one_bit"})
        image_url = first["json"]["input"][0]["content"][1]["image_url"]
        self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))


class MetadataTests(unittest.TestCase):
    def test_location_and_metadata_are_removed(self):
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"
        exif.get_ifd(0x8825)[2] = (36.0, 1.0, 2.0)  # GPS latitude
        source = make_photo(exif=exif.tobytes())
        body = post(create_app(settings(), FakeSam()), source).json()
        self.assertTrue(body["checks"]["metadata_removed"])
        self.assertTrue(body["checks"]["location_removed"])
        result = decode(body)
        self.assertEqual(len(result.getexif()), 0)
        self.assertNotIn("exif", result.info)

    def test_sideways_phone_photo_is_turned_upright(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees
        body = post(
            create_app(settings(), FakeSam(faces=[], plates=[])), make_photo(exif=exif.tobytes())
        ).json()
        self.assertEqual((body["width"], body["height"]), (H, W))

    def test_large_photo_is_shrunk_before_sam(self):
        meta = FakeSam(faces=[], plates=[])
        body = post(create_app(settings(), meta), make_photo(size=(4000, 3000))).json()
        self.assertEqual((body["width"], body["height"]), (2048, 1536))
        self.assertTrue(body["checks"]["resized"])
        sent = meta.calls[0]["json"]["input"][0]["content"][1]["image_url"].split(",", 1)[1]
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(sent))).size, (2048, 1536))

    def test_png_and_webp_are_accepted(self):
        app = create_app(settings(), FakeSam())
        self.assertEqual(post(app, make_photo("PNG"), content_type="image/png").status, 200)
        self.assertEqual(post(app, make_photo("WEBP"), content_type="image/webp").status, 200)


class FailClosedTests(unittest.TestCase):
    def test_a_failed_segmentation_returns_no_photo(self):
        r = post(create_app(settings(), FakeSam(status=500)), make_photo())
        self.assertEqual(r.status, 503)
        self.assertNotIn("image", r.json())

    def test_malformed_records_return_no_photo(self):
        r = post(create_app(settings(), FakeSam(faces_text="<0f>0<|box;x1=5|>")), make_photo())
        self.assertEqual(r.status, 502)
        self.assertNotIn("image", r.json())

    def test_refusal_returns_no_photo(self):
        r = post(create_app(settings(), FakeSam(refuse=True)), make_photo())
        self.assertEqual(r.status, 502)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.meta = FakeSam()
        self.app = create_app(settings(), self.meta)

    def test_requires_token(self):
        self.assertEqual(post(self.app, make_photo(), token=None).status, 401)

    def test_bad_uploads_are_rejected_without_calling_meta(self):
        self.assertEqual(post(self.app, make_photo(), content_type="text/plain").status, 415)
        self.assertEqual(post(self.app, b"not a photo").status, 400)
        self.assertEqual(post(self.app, b"").status, 400)
        self.assertEqual(post(self.app, make_photo(size=(40, 40))).status, 400)
        gif = io.BytesIO()
        Image.new("RGB", (100, 100)).save(gif, format="GIF")
        self.assertEqual(post(self.app, gif.getvalue()).status, 400)
        self.assertEqual(post(self.app, b"x" * (photo.MAX_UPLOAD_BYTES + 1)).status, 413)
        self.assertEqual(self.meta.calls, [])

    def test_points_parsing(self):
        self.assertEqual(
            photo.parse_points("10,20;bad;5000,1;30.5,40", W, H), [(10, 20), (30.5, 40)]
        )

    def test_contributor_sam_model_is_refused(self):
        with self.assertRaises(ConfigError):
            settings(META_SAM_MODEL="sam-3.1-contributor")

    def test_photo_rate_limit(self):
        app = create_app(
            settings(PHOTO_LIMIT_PER_CLIENT_PER_MINUTE="1"), FakeSam(faces=[], plates=[])
        )
        self.assertEqual(post(app, make_photo()).status, 200)
        self.assertEqual(post(app, make_photo()).status, 429)


class MainGroupTests(unittest.TestCase):
    """Real run: a crowded go-kart track kept other drivers whose faces were nearly as large."""

    def face(self, n, box):
        x1, y1, x2, y2 = box
        return photo.Detected(f"F{n}", "face", x1, y1, x2, y2, Image.new("L", (1, 1)), 0.9)

    def test_similar_size_face_far_away_is_not_in_the_group(self):
        faces = [
            self.face(1, (300, 120, 420, 270)),  # customer
            self.face(2, (440, 140, 540, 270)),  # child next to them
            self.face(3, (700, 100, 800, 220)),  # driver in another cart, similar size, far away
            self.face(4, (20, 300, 120, 420)),  # another driver, far the other way
        ]
        self.assertEqual(photo.main_group(faces), {"F1", "F2"})

    def test_group_can_chain_side_by_side(self):
        faces = [self.face(n, (100 + 110 * n, 100, 200 + 110 * n, 220)) for n in range(1, 6)]
        self.assertEqual(len(photo.main_group(faces)), 4)  # never more than MAX_KEPT

    def test_no_faces(self):
        self.assertEqual(photo.main_group([]), set())
