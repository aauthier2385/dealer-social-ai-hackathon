"""Checks a voice-note upload before any of it is sent to Meta.

Muse Voice Transcribe takes a mono 16-bit WAV at 16 or 24 kHz. Browsers convert phone recordings
(.m4a, .webm) into that format before uploading, so the server needs no audio software.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

ACCEPTED_RATES = (16000, 24000)
MAX_SECONDS = 300  # a voice note is a minute or two; 5 minutes is generous
MAX_BYTES = 24000 * 2 * MAX_SECONDS + 1024  # 5 minutes at 24 kHz plus header room
MIN_SECONDS = 1.0


class AudioError(ValueError):
    """The upload is not a usable voice note. The message is safe to show the user."""


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    seconds: float


def check_wav(data: bytes) -> WavInfo:
    if len(data) > MAX_BYTES:
        raise AudioError("The recording is too long. Keep voice notes under 5 minutes.")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("Upload a WAV file (the page converts phone recordings for you).")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate, frames = (
                w.getnchannels(),
                w.getsampwidth(),
                w.getframerate(),
                w.getnframes(),
            )
    except (wave.Error, EOFError) as exc:
        raise AudioError("The WAV file could not be read.") from exc
    if channels != 1 or width != 2 or rate not in ACCEPTED_RATES:
        raise AudioError("The WAV must be mono, 16-bit, at 16 or 24 kHz.")
    seconds = frames / rate
    if seconds < MIN_SECONDS:
        raise AudioError("The recording is too short to contain a voice note.")
    if seconds > MAX_SECONDS:
        raise AudioError("The recording is too long. Keep voice notes under 5 minutes.")
    return WavInfo(sample_rate=rate, seconds=round(seconds, 1))
