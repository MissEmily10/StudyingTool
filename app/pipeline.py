"""Очередь обработки: запись вебинара → распознавание → конспект."""
import asyncio
import traceback
from datetime import datetime, timedelta

from . import config, db, recorder, summarize, transcribe

queue: asyncio.Queue[int]  # создаётся в start() внутри работающего цикла событий
_recording_tasks: dict[int, asyncio.Task] = {}


def audio_path(lesson_id: int):
    return config.lesson_dir(lesson_id) / "audio.webm"


def uploaded_audio(lesson_id: int):
    files = sorted(config.lesson_dir(lesson_id).glob("upload.*"))
    return files[0] if files else None


def materials_path(lesson_id: int):
    return config.lesson_dir(lesson_id) / "materials.pdf"


def _process(lesson_id: int) -> None:
    """Синхронная тяжёлая часть — запускается в отдельном потоке."""
    lesson = db.get(lesson_id)
    progress = lambda msg: db.log(lesson_id, msg)  # noqa: E731

    pdf = None
    mat = materials_path(lesson_id)
    if mat.exists():
        pdf = mat.read_bytes()
    elif lesson["materials_url"]:
        progress("Скачиваю материалы урока")
        try:
            pdf = summarize.fetch_pdf(lesson["materials_url"])
            mat.write_bytes(pdf)
        except Exception as e:
            progress(f"Материалы скачать не удалось ({e}) — продолжаю без них")

    transcript = lesson["transcript"]
    audio = uploaded_audio(lesson_id) or (audio_path(lesson_id) if audio_path(lesson_id).exists() else None)
    if audio and not transcript:
        db.update(lesson_id, status="transcribing")
        minutes = transcribe.duration(audio) / 60
        progress(f"Длительность записи: {minutes:.0f} мин")
        transcript = transcribe.transcribe(audio, config.lesson_dir(lesson_id) / "chunks", progress)
        db.update(lesson_id, transcript=transcript)
        progress("Речь распознана")

    if not transcript and not pdf:
        raise RuntimeError("Нет ни записи, ни материалов — не из чего делать конспект")

    db.update(lesson_id, status="summarizing")
    progress("Пишу конспект")
    konspekt = summarize.build_konspekt(db.get(lesson_id), transcript, pdf)
    db.update(lesson_id, konspekt=konspekt, status="done", error="")
    progress("Конспект готов")


async def worker() -> None:
    while True:
        lesson_id = await queue.get()
        try:
            await asyncio.to_thread(_process, lesson_id)
        except Exception as e:
            db.update(lesson_id, status="error", error=str(e)[:1000])
            db.log(lesson_id, f"Ошибка: {e}")
            traceback.print_exc()
        finally:
            queue.task_done()


def enqueue(lesson_id: int) -> None:
    db.update(lesson_id, status="queued", error="")
    queue.put_nowait(lesson_id)


async def _record_then_process(lesson_id: int) -> None:
    try:
        audio = await recorder.record(lesson_id)
        if audio is None:
            raise RuntimeError("Звук не записался. Посмотрите скриншот бота и журнал ниже")
        enqueue(lesson_id)
    except Exception as e:
        db.update(lesson_id, status="error", error=str(e)[:1000])
        db.log(lesson_id, f"Ошибка записи: {e}")
        traceback.print_exc()
    finally:
        _recording_tasks.pop(lesson_id, None)


async def scheduler() -> None:
    """Раз в 15 секунд запускает запись уроков, у которых подошло время (за 2 минуты до начала)."""
    while True:
        soon = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
        for lesson in db.due_scheduled(soon):
            if lesson["id"] not in _recording_tasks:
                db.update(lesson["id"], status="joining")
                _recording_tasks[lesson["id"]] = asyncio.create_task(_record_then_process(lesson["id"]))
        await asyncio.sleep(15)


def start() -> list[asyncio.Task]:
    global queue
    queue = asyncio.Queue()
    resume_after_restart()
    return [asyncio.create_task(worker()), asyncio.create_task(scheduler())]


def resume_after_restart() -> None:
    """Записи, прерванные перезапуском, всё равно обрабатываем — что успело записаться."""
    for lesson in db.all_lessons():
        if lesson["status"] in ("joining", "recording", "transcribing", "summarizing", "queued"):
            has_audio = audio_path(lesson["id"]).exists() or uploaded_audio(lesson["id"])
            for cap in config.lesson_dir(lesson["id"]).glob("capture_*.webm"):
                if not audio_path(lesson["id"]).exists():
                    cap.replace(audio_path(lesson["id"]))
                    has_audio = True
            if has_audio or lesson["status"] == "queued" or materials_path(lesson["id"]).exists():
                db.log(lesson["id"], "Сервер перезапустился — продолжаю обработку")
                enqueue(lesson["id"])
            else:
                db.update(lesson["id"], status="error", error="Сервер перезапустился во время записи")
