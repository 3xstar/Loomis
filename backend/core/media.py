from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database.models import ProjectMedia
from .voice import VoiceTranscriber


MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}
MAX_MEDIA_BYTES = 5 * 1024 * 1024
transcriber = VoiceTranscriber()


def make_media(project_id: int, filename: str, data: bytes) -> ProjectMedia:
    name = Path(filename.replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    mime_type = MEDIA_TYPES.get(suffix)
    if mime_type is None:
        raise ValueError("Поддерживаются изображения PNG, JPG, WebP и аудио OGG, WebM, MP3, WAV, M4A, FLAC.")
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("Размер медиафайла должен быть от 1 байта до 5 МБ.")
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if mime_type in signatures and not signatures[mime_type]:
        raise ValueError("Содержимое изображения не соответствует расширению файла.")
    return ProjectMedia(project_id=project_id, filename=name[:500], mime_type=mime_type, data=data)


async def transcribe_media(media: ProjectMedia) -> str:
    with TemporaryDirectory() as directory:
        suffix = next(suffix for suffix, mime_type in MEDIA_TYPES.items() if mime_type == media.mime_type)
        path = Path(directory) / f"audio{suffix}"
        path.write_bytes(media.data)
        text = await transcriber.transcribe(path)
        if not text.strip():
            raise ValueError("В аудиофайле не найдена речь.")
        return text


async def media_spec_content(media: ProjectMedia) -> str:
    if media.mime_type.startswith("audio/"):
        return await transcribe_media(media)
    return f"Изображение в приложениях к ТЗ: {media.filename}"
