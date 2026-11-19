import asyncio
from pathlib import Path
from threading import Lock

from backend.settings import settings


class VoiceTranscriber:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.whisper_model
        self._model = None
        self._lock = Lock()

    def _transcribe(self, path: Path) -> str:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(str(path))
            return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, path: str | Path) -> str:
        path = Path(path)
        if path.suffix.lower() not in {".ogg", ".mp3", ".wav", ".m4a", ".flac"}:
            raise ValueError("Поддерживаются аудиофайлы .ogg, .mp3, .wav, .m4a и .flac.")
        return await asyncio.to_thread(self._transcribe, path)
