import re
from io import BytesIO
import wave


def refuses_media(message):
    return bool(re.search(
        r"(?:(?:cannot|can['’]t|unable to)\s+(?:see|view|access|hear|process|analy[sz]e|read|listen to)"
        r"|do not have access to|don['’]t see)\s+"
        r"(?:(?:the|this|your|any|an?|attached|provided|uploaded)\s+){0,4}"
        r"(?:image|audio|attachment|picture|media|recording)"
        r"|(?:не вижу|не получил[аи]?|не могу\s+(?:увидеть|открыть|прослушать|обработать|проанализировать))"
        r"\s+(?:(?:ваш[еуо]?|эт[оуие]|прикрепл[её]нн\w+|вложенн\w+)\s+){0,3}"
        r"(?:изображени|аудио|вложени|картинк|запис)"
        r"|(?:image|audio|image_url|input_audio).{0,40}(?:not supported|unsupported)"
        r"|(?:unsupported|does not support|не поддержива\w*).{0,40}"
        r"(?:image|audio|изображени|аудио)"
        r"|(?:no|did not receive any) (?:image|audio|attachment) (?:was |is )?(?:provided|attached|received)",
        message, re.I,
    ))


def audio_payload(mime_type, data):
    if mime_type in {"audio/wav", "audio/mpeg"}:
        return ("wav" if mime_type == "audio/wav" else "mp3"), data

    import av

    output = BytesIO()
    with av.open(BytesIO(data)) as source, wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in source.decode(audio=0):
            for pcm in resampler.resample(frame):
                wav.writeframes(bytes(pcm.planes[0])[:pcm.samples * 2])
        for pcm in resampler.resample(None):
            wav.writeframes(bytes(pcm.planes[0])[:pcm.samples * 2])
    return "wav", output.getvalue()
