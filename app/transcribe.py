"""Распознавание речи: аудио → текст с таймкодами [мм:сс]."""
import subprocess
from pathlib import Path

import httpx

from . import config

CHUNK_SECONDS = 600  # облачные API принимают файлы до ~25 МБ — режем по 10 минут


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        # webm из MediaRecorder бывает без длительности в заголовке — считаем по декодированию
        out = subprocess.run(["ffmpeg", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True).stderr
        times = [line.split("time=")[-1].split()[0] for line in out.splitlines() if "time=" in line]
        if not times:
            return 0.0
        h, m, s = times[-1].split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)


def to_mono_chunks(src: Path, workdir: Path) -> list[tuple[Path, float]]:
    """Перекодируем в моно 16 кГц mp3 и режем на куски. Возвращаем (файл, сдвиг в секундах)."""
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("chunk_*.mp3"):
        old.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
         "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
         str(workdir / "chunk_%03d.mp3")],
        check=True,
    )
    chunks = sorted(workdir.glob("chunk_*.mp3"))
    return [(c, i * CHUNK_SECONDS) for i, c in enumerate(chunks)]


def _api_segments(chunk: Path) -> list[tuple[float, str]]:
    if not config.TRANSCRIBE_API_KEY:
        raise RuntimeError("Не задан TRANSCRIBE_API_KEY — нечем распознавать речь (см. .env)")
    with open(chunk, "rb") as f:
        resp = httpx.post(
            f"{config.TRANSCRIBE_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.TRANSCRIBE_API_KEY}"},
            data={"model": config.TRANSCRIBE_MODEL, "language": "ru", "response_format": "verbose_json",
                  "timestamp_granularities[]": "segment"},
            files={"file": (chunk.name, f, "audio/mpeg")},
            timeout=600,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Сервис распознавания ответил {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    segs = body.get("segments") or []
    if not segs and body.get("text"):
        return [(0.0, body["text"])]
    return [(float(s["start"]), s["text"]) for s in segs]


_local_model = None


def _local_segments(chunk: Path) -> list[tuple[float, str]]:
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel
        _local_model = WhisperModel(config.LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8",
                                    cpu_threads=config.WHISPER_THREADS)
    segments, _ = _local_model.transcribe(str(chunk), language="ru", vad_filter=True)
    return [(s.start, s.text) for s in segments]


def transcribe(audio: Path, workdir: Path, progress=lambda msg: None) -> str:
    chunks = to_mono_chunks(audio, workdir)
    if not chunks:
        raise RuntimeError("В записи нет звука")
    lines = []
    for n, (chunk, offset) in enumerate(chunks, 1):
        progress(f"Распознаю часть {n} из {len(chunks)}")
        segs = _local_segments(chunk) if config.TRANSCRIBE_PROVIDER == "local" else _api_segments(chunk)
        lines += [f"[{fmt_ts(offset + start)}] {text.strip()}" for start, text in segs if text.strip()]
    if not lines:
        raise RuntimeError("Речь не распознана — возможно, в записи тишина")
    return "\n".join(lines)
