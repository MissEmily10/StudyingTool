"""Очередь обработки: запись вебинара → распознавание → конспект."""
import asyncio
import traceback
from datetime import datetime, timedelta

from . import config, db, recorder, summarize, sync, transcribe

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

    if config.SUMMARY_MODE != "api":
        _hand_over(lesson_id, transcript, mat if mat.exists() else None)
        return

    db.update(lesson_id, status="summarizing")
    progress("Пишу конспект")
    konspekt = summarize.build_konspekt(db.get(lesson_id), transcript, pdf)
    db.update(lesson_id, konspekt=konspekt, status="done", error="")
    progress("Конспект готов")


def _hand_over(lesson_id: int, transcript: str, materials) -> None:
    """Режим без API: отдаём расшифровку Claude через репозиторий и ждём konspekt.md."""
    db.update(lesson_id, status="waiting", error="")
    if not sync.enabled():
        db.log(lesson_id, "GitHub не настроен: скачайте расшифровку и отправьте её в чат Claude, "
                          "готовый конспект вставьте через «Вставить конспект»")
        return
    rel = sync.publish(db.get(lesson_id), transcript, materials)
    db.log(lesson_id, f"Расшифровка отправлена в репозиторий ({rel}) — конспект появится, когда его соберёт Claude")


async def sync_loop() -> None:
    """Раз в несколько минут забираем из репозитория готовые конспекты."""
    while True:
        await asyncio.sleep(config.SYNC_MINUTES * 60)
        # ждём конспект, а у готовых уроков — шпору, если её дописали позже
        if not sync.enabled():
            continue
        try:
            await sync_once()
        except Exception as e:
            print("sync:", e)


async def sync_once() -> None:
    """Один проход обмена с репозиторием: конспекты/шпоры/тесты/видео и вопросы к Claude."""
    from . import bot  # здесь, чтобы не было циклического импорта
    # ждём конспект, а у готовых уроков — шпору, тест и видео, если их дописали позже
    waiting = [l["id"] for l in db.all_lessons()
               if l["status"] == "waiting" or (l["status"] == "done" and not (l["shpora"] and l["quiz"] and l["videos"]))]
    if waiting:
        found = await asyncio.to_thread(sync.collect_konspekts)
        for lesson_id in waiting:
            got = found.get(lesson_id)
            if not got:
                continue
            lesson = db.get(lesson_id)
            extras = {k: got[k] for k in ("shpora", "quiz", "videos") if got[k] and not lesson[k]}
            if lesson["status"] == "waiting":
                db.update(lesson_id, konspekt=got["konspekt"], status="done", error="", **extras)
                db.log(lesson_id, "Конспект получен")
                bot.on_lesson_done(lesson_id)
            elif extras:
                db.update(lesson_id, **extras)
                db.log(lesson_id, "Получено: " + ", ".join(extras))

    new_q = db.questions("new")
    if new_q:
        for q in new_q:
            lesson = db.get(q["lesson_id"]) if q["lesson_id"] else None
            q["lesson_folder"] = f"lessons/{sync.folder_name(lesson)}" if lesson else ""
        await asyncio.to_thread(sync.publish_questions, new_q)
        for q in new_q:
            db.set_question_status(q["id"], "asked")
    asked = db.questions("asked")
    if asked:
        answers = await asyncio.to_thread(sync.collect_answers)
        for q in asked:
            if q["id"] in answers:
                db.answer_question(q["id"], answers[q["id"]])
                q["answer"] = answers[q["id"]]
                bot.on_answer(q)


async def worker() -> None:
    while True:
        lesson_id = await queue.get()
        try:
            await asyncio.to_thread(_process, lesson_id)
            if db.get(lesson_id)["status"] == "done":
                from . import bot
                bot.on_lesson_done(lesson_id)
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


def _schedule_next_week(lesson: dict) -> None:
    """Повторяющийся урок: ставим такую же запись через неделю, а у текущей снимаем повтор."""
    nxt = (datetime.fromisoformat(lesson["starts_at"]) + timedelta(days=7)).isoformat(timespec="seconds")
    new_id = db.create(
        subject=lesson["subject"], teacher=lesson["teacher"], webinar_url=lesson["webinar_url"],
        starts_at=nxt, status="scheduled", wait_minutes=lesson["wait_minutes"], repeat_weekly=1,
    )
    db.update(lesson["id"], repeat_weekly=0)
    db.log(lesson["id"], f"Следующая запись поставлена на {nxt[:16].replace('T', ' ')} (урок №{new_id})")


async def scheduler() -> None:
    """Раз в 15 секунд запускает запись уроков, у которых подошло время (за 2 минуты до начала)."""
    while True:
        soon = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
        for lesson in db.due_scheduled(soon):
            if lesson["id"] not in _recording_tasks:
                if lesson.get("repeat_weekly"):
                    _schedule_next_week(lesson)
                db.update(lesson["id"], status="joining")
                _recording_tasks[lesson["id"]] = asyncio.create_task(_record_then_process(lesson["id"]))
        await asyncio.sleep(15)


def start() -> list[asyncio.Task]:
    global queue
    queue = asyncio.Queue()
    resume_after_restart()
    return [asyncio.create_task(worker()), asyncio.create_task(scheduler()), asyncio.create_task(sync_loop())]


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
