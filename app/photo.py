"""Feature 5: privacy blur for store photos, with SAM 3.1.

Dealerships post delivery-day photos of happy customers. Those photos often catch other people
in the background and license plates. Before a photo is posted:

  1. The photo is checked, turned upright, shrunk to a sensible size and stripped of its
     metadata (including GPS location) on this server.
  2. SAM 3.1 finds every "face" and every "license plate", with pixel masks.
  3. Code decides what to blur. The main subjects (the largest faces: the customer and their
     family) are kept; everyone smaller in the background is blurred; every license plate is
     blurred. Staff can tap a face to keep it or blur it, and the photo is rendered again.
  4. Blurring is strong (pixelated, then smoothed) over each mask, grown a little and joined
     with an oval over the face's box, so a loose mask cannot leave part of a face showing.

It fails closed: if a segmentation call fails, no photo is returned at all.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import anyio
from meta_sam_parser import (
    CompletedOutcome,
    InvalidSegmentationMaskError,
    SegmentationMaskRecord,
    decode_mask_to_raster,
    image_segmentation_format,
)
from PIL import Image, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from app.meta_client import MetaBadResponse, MetaClient

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_SOURCE_PIXELS = 50_000_000  # refuses decompression bombs
MAX_SIDE = 2048  # long side sent to SAM and returned
MIN_SIDE = 64
FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}  # MPO: some phones save JPEGs this way
KEEP_RATIO = 0.4  # a face at least 40% the size of the largest face is a main subject
MAX_KEPT = 4
MAX_POINTS = 12

Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS


class PhotoError(ValueError):
    """The upload is not a usable photo. The message is safe to show."""


@dataclass
class Detected:
    id: str
    kind: str  # "face" or "plate"
    left: int
    top: int
    right: int
    bottom: int
    mask: Image.Image  # "L" mode, full photo size, 255 = object
    confidence: float | None
    decision: str = ""
    reason: str = ""

    @property
    def area(self) -> int:
        return (self.right - self.left) * (self.bottom - self.top)

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def prepare(data: bytes) -> tuple[Image.Image, dict]:
    """Open, check, turn upright, shrink and strip a photo. Returns (RGB image, notes)."""
    if not data:
        raise PhotoError("Choose a photo first.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("That photo is too large. Keep it under 12 MB.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in FORMATS:
                raise PhotoError("Send a JPEG, PNG or WebP photo.")
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise PhotoError("That file is not a photo this service can read.") from exc
    exif = image.getexif()
    had_location = bool(exif.get_ifd(0x8825)) if exif else False  # GPS IFD
    had_metadata = bool(exif) or bool(image.info.get("exif")) or bool(image.info.get("xmp"))
    image = ImageOps.exif_transpose(image).convert("RGB")
    if min(image.size) < MIN_SIDE:
        raise PhotoError("That photo is too small.")
    resized = max(image.size) > MAX_SIDE
    if resized:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    notes = {
        "width": image.width,
        "height": image.height,
        "resized": resized,
        "metadata_removed": had_metadata,
        "location_removed": had_location,
    }
    return image, notes


def to_jpeg(image: Image.Image, quality: int = 90) -> bytes:
    """Encode without any metadata."""
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def parse_masks(text: str, kind: str, size: tuple[int, int]) -> list[Detected]:
    """Turn SAM's segmentation records into full-size masks. Any malformed record is an error."""
    parser = image_segmentation_format().create_parser()
    parser.push(text)
    result = parser.finish(CompletedOutcome()).result
    errors = [d for d in result.diagnostics if d.severity == "error"]
    if errors:
        raise MetaBadResponse(f"segmentation records were malformed ({errors[0].code})")
    width, height = size
    found: list[Detected] = []
    for record in result.records:
        if not isinstance(record, SegmentationMaskRecord):
            continue
        try:
            raster = decode_mask_to_raster(record.mask)
        except InvalidSegmentationMaskError as exc:
            raise MetaBadResponse("a segmentation mask could not be decoded") from exc
        left = max(0, int(record.bounds.left))
        top = max(0, int(record.bounds.top))
        right = min(width, int(round(record.bounds.right)))
        bottom = min(height, int(round(record.bounds.bottom)))
        if right <= left or bottom <= top:
            continue
        local = Image.frombytes(
            "L", (record.mask.width, record.mask.height), bytes(255 if b else 0 for b in raster)
        )
        if local.size != (right - left, bottom - top):
            local = local.resize((right - left, bottom - top), Image.Resampling.NEAREST)
        full = Image.new("L", size, 0)
        full.paste(local, (left, top))
        prefix = "F" if kind == "face" else "P"
        found.append(
            Detected(
                id=f"{prefix}{len(found) + 1}",
                kind=kind,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                mask=full,
                confidence=record.confidence,
            )
        )
    return found


def decide(
    faces: list[Detected],
    plates: list[Detected],
    keep_points: list[tuple[float, float]],
    blur_points: list[tuple[float, float]],
    blur_all_faces: bool,
) -> None:
    """Choose what to blur. Faces: the main subjects stay, background people are blurred.
    A tap on a face overrides the default. License plates are always blurred."""
    largest = max((f.area for f in faces), default=0)
    by_size = sorted(faces, key=lambda f: f.area, reverse=True)
    main = {f.id for f in by_size[:MAX_KEPT] if largest and f.area >= KEEP_RATIO * largest}
    for face in faces:
        if any(face.contains(x, y) for x, y in blur_points):
            face.decision, face.reason = "blurred", "you chose to blur"
        elif any(face.contains(x, y) for x, y in keep_points):
            face.decision, face.reason = "kept", "you chose to keep"
        elif blur_all_faces:
            face.decision, face.reason = "blurred", "blur all faces is on"
        elif face.id in main:
            face.decision, face.reason = "kept", "main subject"
        else:
            face.decision, face.reason = "blurred", "background person"
    for plate in plates:
        plate.decision, plate.reason = "blurred", "license plate"


def _grown(mask: Image.Image, box: tuple[int, int, int, int], oval: bool) -> Image.Image:
    """The mask grown a little, plus an oval over the box for faces, with soft edges."""
    left, top, right, bottom = box
    side = max(right - left, bottom - top)
    grow = max(3, (side // 12) | 1)  # odd kernel size for MaxFilter
    region = mask.filter(ImageFilter.MaxFilter(grow))
    if oval:
        pad_x, pad_y = (right - left) * 0.12, (bottom - top) * 0.12
        ImageDraw.Draw(region).ellipse(
            (left - pad_x, top - pad_y, right + pad_x, bottom + pad_y), fill=255
        )
    return region.filter(ImageFilter.GaussianBlur(max(1, side / 40)))


def blur(image: Image.Image, objects: list[Detected]) -> Image.Image:
    """Pixelate then smooth every object marked "blurred", inside its grown mask."""
    out = image.copy()
    for obj in objects:
        if obj.decision != "blurred":
            continue
        side = max(obj.right - obj.left, obj.bottom - obj.top)
        pad = int(side * 0.25)
        crop_box = (
            max(0, obj.left - pad),
            max(0, obj.top - pad),
            min(image.width, obj.right + pad),
            min(image.height, obj.bottom + pad),
        )
        region = out.crop(crop_box)
        blocks = max(1, side // 10)  # about 10 blocks across the object
        small = region.resize(
            (max(1, region.width // blocks), max(1, region.height // blocks)),
            Image.Resampling.BILINEAR,
        )
        hidden = small.resize(region.size, Image.Resampling.NEAREST).filter(
            ImageFilter.GaussianBlur(max(2, blocks / 2))
        )
        alpha = _grown(obj.mask, (obj.left, obj.top, obj.right, obj.bottom), obj.kind == "face")
        out.paste(hidden, crop_box[:2], alpha.crop(crop_box))
    return out


def parse_points(raw: str, width: int, height: int) -> list[tuple[float, float]]:
    """'x,y;x,y' in photo pixels. Points outside the photo are ignored."""
    points: list[tuple[float, float]] = []
    for part in (raw or "").split(";")[:MAX_POINTS]:
        try:
            x, y = (float(v) for v in part.split(","))
        except ValueError:
            continue
        if 0 <= x < width and 0 <= y < height:
            points.append((x, y))
    return points


def _box(d: Detected, width: int, height: int) -> dict:
    return {
        "id": d.id,
        "box": {"left": d.left, "top": d.top, "right": d.right, "bottom": d.bottom},
        "area_pct": round(100 * d.area / (width * height), 2),
        "confidence": d.confidence,
        "decision": d.decision,
        "reason": d.reason,
    }


async def privacy_blur(
    meta: MetaClient,
    data: bytes,
    keep: str = "",
    blur_points: str = "",
    blur_all_faces: bool = False,
) -> dict:
    image, notes = await anyio.to_thread.run_sync(prepare, data)
    jpeg = await anyio.to_thread.run_sync(to_jpeg, image)

    results: dict[str, object] = {}

    async def run(concept: str, kind: str) -> None:
        results[kind] = await meta.segment(jpeg, "image/jpeg", concept)

    # Both questions at once; if either fails, the whole request fails (no unblurred photo).
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run, "face", "face")
            tg.start_soon(run, "license plate", "plate")
    except BaseExceptionGroup as group:
        raise group.exceptions[0] from None

    size = image.size
    faces = await anyio.to_thread.run_sync(parse_masks, results["face"].text, "face", size)
    plates = await anyio.to_thread.run_sync(parse_masks, results["plate"].text, "plate", size)
    decide(
        faces,
        plates,
        parse_points(keep, *size),
        parse_points(blur_points, *size),
        blur_all_faces,
    )
    blurred = await anyio.to_thread.run_sync(blur, image, faces + plates)
    out = await anyio.to_thread.run_sync(to_jpeg, blurred, 88)

    usage: dict[str, int] = {}
    for r in results.values():
        for k, v in r.usage.items():
            usage[k] = usage.get(k, 0) + v
    return {
        "image": "data:image/jpeg;base64," + base64.b64encode(out).decode(),
        "width": size[0],
        "height": size[1],
        "faces": [_box(f, *size) for f in faces],
        "plates": [_box(p, *size) for p in plates],
        "checks": {
            "faces_found": len(faces),
            "faces_blurred": sum(f.decision == "blurred" for f in faces),
            "faces_kept": sum(f.decision == "kept" for f in faces),
            "plates_found": len(plates),
            "plates_blurred": len(plates),
            "metadata_removed": notes["metadata_removed"],
            "location_removed": notes["location_removed"],
            "resized": notes["resized"],
        },
        "usage": usage,
        "latency_ms": max(r.latency_ms for r in results.values()),
        "model": results["face"].model,
    }
