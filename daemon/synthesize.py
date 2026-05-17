"""Render a script to a WAV file using Kokoro (local ONNX TTS)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODEL_PATH = MODELS_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODELS_DIR / "voices-v1.0.bin"

# Pick a default voice. `af_heart` is the warm, neutral default from Kokoro v1.
# To change, set PODCAST_VOICE env var. Other good picks: af_bella, am_michael, bf_emma.
DEFAULT_VOICE = "af_heart"

_kokoro: Kokoro | None = None


def _get_kokoro() -> Kokoro:
    global _kokoro
    if _kokoro is None:
        if not MODEL_PATH.exists() or not VOICES_PATH.exists():
            raise RuntimeError(
                f"Kokoro model files missing. Run daemon/setup.sh to download them.\n"
                f"  expected: {MODEL_PATH}\n"
                f"  expected: {VOICES_PATH}"
            )
        _kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    return _kokoro


def _split_into_chunks(text: str, max_chars: int = 500) -> list[str]:
    """Split on sentence boundaries; Kokoro chokes on very long inputs."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if len(buf) + len(s) + 1 <= max_chars:
            buf = (buf + " " + s).strip() if buf else s
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks


def synthesize(script: str, out_path: Path, voice: str | None = None) -> float:
    """Render `script` to `out_path` (WAV). Returns duration in seconds."""
    kokoro = _get_kokoro()
    voice = voice or os.environ.get("PODCAST_VOICE", DEFAULT_VOICE)

    chunks = _split_into_chunks(script)
    if not chunks:
        raise RuntimeError("empty script")

    audio_parts: list[np.ndarray] = []
    sample_rate = 24000
    for chunk in chunks:
        samples, sample_rate = kokoro.create(chunk, voice=voice, speed=1.0, lang="en-us")
        audio_parts.append(samples)
        # tiny silence between chunks so sentences don't collide
        audio_parts.append(np.zeros(int(sample_rate * 0.15), dtype=np.float32))

    audio = np.concatenate(audio_parts)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), audio, sample_rate)
    return len(audio) / sample_rate
