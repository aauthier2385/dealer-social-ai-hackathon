"""Feature 6: "Truckiversary" posts. A delivery-day photo becomes a one-year anniversary post.

On the anniversary of a purchase, the salesperson posts a celebration card for the customer:

  1. The delivery photo is prepared on this server (upright, shrunk, all metadata including GPS
     removed). If asked, bystanders' faces and license plates are blurred first with SAM 3.1.
  2. Muse Image paints a celebration scene for the customer's vehicle: open road, confetti,
     balloons. It is told to draw no people, no text and no logos.
  3. Code builds the card: the scene as the background, the REAL delivery photo placed on top
     untouched, and the words drawn by code. The model never repaints a customer's face and
     never spells their name.
  4. Muse Spark writes a short caption in the salesperson's own texting voice. Code checks it:
     it must name the customer, fit in a post, and avoid anything sensitive. Personal facts are
     never used: this is a public post.

Nothing is created without the customer's OK to be featured.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass

import anyio
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from app import photo
from app.memory import sensitive_type
from app.meta_client import MetaBadResponse, MetaClient
from app.planner import DEFAULT_VOICE, PlanInputError, _text, custom_voice, voice_samples

CARD_W, CARD_H = 1080, 1350  # portrait, the shape social feeds show best
MAX_PHOTO_CHARS = 18 * 1024 * 1024  # a 12 MB photo as base64
MAX_CAPTION = 280
TRUCK_WORDS = re.compile(
    r"\b(truck|pickup|f-?150|f-?250|f-?350|super duty|ranger|maverick|silverado|sierra|ram|"
    r"tundra|tacoma|colorado|canyon|frontier|titan|ridgeline|gladiator|cybertruck|lightning)\b",
    re.IGNORECASE,
)
_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}


@dataclass
class CardInput:
    photo: bytes
    first_name: str
    vehicle: str
    color: str
    store: str
    salesperson: str
    years: int
    blur_bystanders: bool
    voice_samples: list[str]
    sample_names: frozenset

    @property
    def is_truck(self) -> bool:
        return bool(TRUCK_WORDS.search(self.vehicle))

    @property
    def ordinal(self) -> str:
        return _ORDINAL.get(self.years, f"{self.years}th")

    @property
    def headline(self) -> str:
        kind = "Truckiversary" if self.is_truck else "Anniversary"
        return f"Happy {self.ordinal} {kind}, {self.first_name}!"


def _decode_photo(raw) -> bytes:
    if not isinstance(raw, str) or not raw:
        raise PlanInputError("'photo' is required: the delivery photo as a data URL.")
    if len(raw) > MAX_PHOTO_CHARS:
        raise PlanInputError("That photo is too large. Keep it under 12 MB.")
    payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise PlanInputError("'photo' is not a valid image.") from exc


def parse_input(data: dict) -> CardInput:
    if data.get("customer_consent") is not True:
        raise PlanInputError(
            "Get the customer's OK to be featured first, then tick the consent box."
        )
    years = data.get("years", 1)
    if not isinstance(years, int) or isinstance(years, bool) or not 1 <= years <= 30:
        raise PlanInputError("'years' must be a whole number from 1 to 30.")
    blur = data.get("blur_bystanders", True)
    if not isinstance(blur, bool):
        raise PlanInputError("'blur_bystanders' must be true or false.")
    first = _text(data, "customer_first_name", 40)
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'’-]{0,39}", first):
        raise PlanInputError("'customer_first_name' must be a name.")
    vehicle = _text(data, "vehicle", 60)
    color = _text(data, "vehicle_color", 30, required=False)
    for value in (vehicle, color):
        if sensitive_type(value) or re.search(r"[<>{}|\\]", value):
            raise PlanInputError("Describe the vehicle in plain words.")
    samples, names = voice_samples(data)
    return CardInput(
        photo=_decode_photo(data.get("photo")),
        first_name=first.split()[0],
        vehicle=vehicle,
        color=color,
        store=_text(data, "store_name", 60, required=False) or "Demo Motors",
        salesperson=_text(data, "salesperson_name", 60),
        years=years,
        blur_bystanders=blur,
        voice_samples=samples,
        sample_names=names,
    )


def scene_prompt(card: CardInput) -> str:
    vehicle = f"{card.color} {card.vehicle}".strip()
    return (
        f"A bright, joyful celebration illustration for a customer's {card.ordinal} anniversary "
        f"of owning their {vehicle}. Show the {vehicle} large in the lower third of the "
        "picture, on an open country road at golden hour. The upper two thirds are a warm sky "
        "with confetti, a few balloons and streamers, kept calm and simple because a photo "
        "will be placed over it. Portrait orientation. Absolutely no people, no faces, no "
        "hands, no text, no letters, no numbers, no logos, no badges and no license plates."
    )


CAPTION_SYSTEM = """You write one short public social media post for a car dealership \
salesperson, celebrating a customer's vehicle anniversary. It goes on the store's feed with a \
photo of the customer.

The salesperson's voice:
<<VOICE>>

Rules:
- Greet the customer by first name and say how many years it has been with their vehicle.
- Warm and celebratory, no sales pitch, no prices, no offers, no service reminders.
- Never mention anything personal or private (family, health, money, where they live, work). \
Only the customer's first name, the vehicle and the anniversary.
- Under 240 characters. Emojis are fine if they fit the voice.
- Sign with the salesperson's first name."""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {"caption": {"type": "string"}},
    "required": ["caption"],
    "additionalProperties": False,
}

_BUSINESS = re.compile(
    r"\b(price|payment|deal|offer|discount|financ\w*|rebate|special|service|trade[- ]?in|"
    r"lease|upgrade|inventory|sale|apr|\$\d)",
    re.IGNORECASE,
)


def caption_problem(text: str, card: CardInput) -> str:
    if not text.strip():
        return "empty"
    if len(text) > MAX_CAPTION:
        return "too_long"
    if sensitive_type(text):
        return "sensitive"
    if _BUSINESS.search(text):
        return "sales_pitch"
    if card.first_name.lower() not in text.lower():
        return "missing_name"
    words = {w.lower() for w in re.findall(r"[A-Za-z'’-]+", text)}
    if (card.sample_names - {card.first_name.lower()}) & words:
        return "name_from_voice_sample"
    if re.search(r"[{}<>]", text):
        return "placeholder"
    return ""


def fallback_caption(card: CardInput) -> str:
    kind = "Truckiversary" if card.is_truck else "anniversary"
    years = "a year" if card.years == 1 else f"{card.years} years"
    sp = card.salesperson.split()[0]
    return (
        f"Happy {card.ordinal} {kind}, {card.first_name}! 🎉 Can't believe it's been {years} "
        f"with your {card.vehicle}. Thanks for being part of the {card.store} family! - {sp}"
    )


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, smallest: int):
    size = start
    while size > smallest:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(smallest)


def build_card(scene: Image.Image, delivery: Image.Image, card: CardInput) -> Image.Image:
    """The scene as background, the real photo on top, and the words drawn by code."""
    canvas = ImageOps.fit(scene.convert("RGB"), (CARD_W, CARD_H), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # The real delivery photo, untouched, as a framed print with a soft shadow.
    framed = delivery.copy()
    framed.thumbnail((880, 620), Image.Resampling.LANCZOS)
    border = 18
    frame = Image.new("RGB", (framed.width + 2 * border, framed.height + 2 * border), "white")
    frame.paste(framed, (border, border))
    x = (CARD_W - frame.width) // 2
    y = 240 + (640 - frame.height) // 2  # upper two thirds; the vehicle shows below it
    shadow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (x + 10, y + 14, x + frame.width + 10, y + frame.height + 14), fill=(0, 0, 0, 110)
    )
    canvas.paste(
        shadow.filter(ImageFilter.GaussianBlur(14)),
        (0, 0),
        shadow.filter(ImageFilter.GaussianBlur(14)),
    )
    canvas.paste(frame, (x, y))

    # Headline at the top, details at the bottom, on soft dark bands so they read on any scene.
    headline_font = _fit_text(draw, card.headline, CARD_W - 120, 76, 40)
    draw.rounded_rectangle((40, 60, CARD_W - 40, 210), 28, fill=(10, 20, 40, 170))
    draw.text((CARD_W / 2, 135), card.headline, font=headline_font, fill="white", anchor="mm")

    vehicle = f"{card.color} {card.vehicle}".strip()
    line1 = f"{card.years} year{'s' if card.years > 1 else ''} with the {vehicle}"
    line2 = f"{card.store}  ·  from {card.salesperson.split()[0]}"
    draw.rounded_rectangle((40, CARD_H - 180, CARD_W - 40, CARD_H - 30), 28, fill=(10, 20, 40, 170))
    draw.text(
        (CARD_W / 2, CARD_H - 132),
        line1,
        font=_fit_text(draw, line1, CARD_W - 140, 48, 28),
        fill="white",
        anchor="mm",
    )
    draw.text(
        (CARD_W / 2, CARD_H - 72),
        line2,
        font=_fit_text(draw, line2, CARD_W - 140, 38, 24),
        fill=(255, 225, 140),
        anchor="mm",
    )
    return canvas


def _open_scene(data: bytes) -> Image.Image:
    try:
        scene = Image.open(io.BytesIO(data))
        scene.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MetaBadResponse("the generated image could not be read") from exc
    return scene.convert("RGB")


async def make_card(meta: MetaClient, card: CardInput) -> dict:
    delivery, notes = await anyio.to_thread.run_sync(photo.prepare, card.photo)
    usage: dict[str, int] = {}
    blur_checks = {"faces_blurred": 0, "plates_blurred": 0}

    def add(u: dict) -> None:
        for k, v in u.items():
            usage[k] = usage.get(k, 0) + v

    if card.blur_bystanders:
        delivery, faces, plates, u, _, _ = await photo.find_and_blur(meta, delivery)
        add(u)
        blur_checks = {
            "faces_blurred": sum(f.decision == "blurred" for f in faces),
            "plates_blurred": len(plates),
        }

    results: dict[str, object] = {}
    voice = custom_voice(card.voice_samples) if card.voice_samples else DEFAULT_VOICE
    user = (
        f"Customer first name: {card.first_name}\nVehicle: {card.color} {card.vehicle}\n"
        f"Years: {card.years}\nStore: {card.store}\nSalesperson: {card.salesperson}"
    )

    async def scene() -> None:
        results["scene"] = await meta.generate_image(scene_prompt(card))

    async def caption() -> None:
        results["caption"] = await meta.chat_json(
            [
                {"role": "system", "content": CAPTION_SYSTEM.replace("<<VOICE>>", voice)},
                {"role": "user", "content": user},
            ],
            CAPTION_SCHEMA,
            "anniversary_caption",
            reasoning_effort="minimal",
        )

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(scene)
            tg.start_soon(caption)
    except BaseExceptionGroup as group:
        raise group.exceptions[0] from None

    scene_bytes, scene_result = results["scene"]
    caption_data, caption_result = results["caption"]
    add(scene_result.usage)
    add(caption_result.usage)

    scene_image = await anyio.to_thread.run_sync(_open_scene, scene_bytes)
    built = await anyio.to_thread.run_sync(build_card, scene_image, delivery, card)
    out = await anyio.to_thread.run_sync(photo.to_jpeg, built, 90)

    text = " ".join(str(caption_data.get("caption", "")).split())
    problem = caption_problem(text, card)
    if problem:
        text = fallback_caption(card)

    return {
        "image": "data:image/jpeg;base64," + base64.b64encode(out).decode(),
        "width": CARD_W,
        "height": CARD_H,
        "caption": text,
        "headline": card.headline,
        "checks": {
            "photo_untouched_by_ai": True,
            "bystanders_blurred": card.blur_bystanders,
            **blur_checks,
            "metadata_removed": notes["metadata_removed"],
            "location_removed": notes["location_removed"],
            "caption_replaced_with_safe_draft": bool(problem),
            "caption_problem": problem,
        },
        "voice": {
            "source": "salesperson_samples" if card.voice_samples else "store_default",
            "samples_used": len(card.voice_samples),
        },
        "usage": usage,
        "latency_ms": max(scene_result.latency_ms, caption_result.latency_ms),
        "models": {"image": scene_result.model, "caption": caption_result.model},
    }
