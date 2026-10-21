"""Voice notes to text, on this machine.

faster-whisper runs the Whisper model locally, so a voice note never leaves
the machine and needs no second API key. It decodes Telegram's OGG/Opus
itself (through PyAV), so there is no ffmpeg to install. The model is
downloaded on first use (`base.en` is ~150 MB) and kept loaded after that.

Needs the `voice` extra; without it the bot answers voice notes by saying so.
"""

from pathlib import Path
from threading import Lock
from typing import Any


class VoiceUnavailable(RuntimeError):
    """The `voice` extra isn't installed."""


class Transcriber:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock = Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    raise VoiceUnavailable(
                        "Voice notes need the voice extra: "
                        "uv sync --package tee-time-booker --extra bot --extra voice"
                    ) from None
                # int8 on the CPU: fast enough for a few seconds of speech on
                # any recent laptop, and no GPU setup.
                self._model = WhisperModel(
                    self._model_name, device="cpu", compute_type="int8"
                )
            return self._model

    def transcribe(self, audio: Path) -> str:
        """Blocking; run it in a thread."""
        model = self._load()
        segments, _info = model.transcribe(_decode(audio), vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


def _decode(path: Path) -> Any:
    """The file as 16 kHz mono float samples, which is what Whisper takes.

    faster-whisper can open files itself, but 1.2.1 passes PyAV an argument
    PyAV 15+ removed, so the decoding is done here and the samples handed over.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0
