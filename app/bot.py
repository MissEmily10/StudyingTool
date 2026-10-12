"""Telegram-бот StudyTool — личный: отвечает только владельцу (первому, кто написал /start)."""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Poll,
                      ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import botlogic, config, db, pipeline, printing

log = logging.getLogger("studytool.bot")

BTN_NOTES, BTN_TODAY, BTN_ASK, BTN_ADD = "📚 Конспекты", "📅 Сегодня", "❓ Спросить", "➕ Добавить урок"
MENU = ReplyKeyboardMarkup([[KeyboardButton(BTN_NOTES), KeyboardButton(BTN_TODAY)],
                            [KeyboardButton(BTN_ASK), KeyboardButton(BTN_ADD)]], resize_keyboard=True)
HELP = (
    "Я присылаю конспекты, шпоры и тесты после уроков.\n\n"
    f"{BTN_NOTES} — готовые конспекты: PDF, шпоры A6/A7, тест, видео.\n"
    f"{BTN_TODAY} — уроки на сегодня.\n"
    f"{BTN_ASK} — вопрос по материалу: быстрый ответ сразу, сложный — Claude.\n"
    f"{BTN_ADD} — пришли PDF-презентацию, аудио или видео урока (до 20 МБ) "
    "с подписью «Предмет. Тема» — сделаю конспект."
)

_app: Application | None = None


def owner_id() -> int | None:
    v = db.get_setting("telegram_owner")
    return int(v) if v else None


def _is_owner(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == owner_id()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if owner_id() is None:
        db.set_setting("telegram_owner", str(user.id))
        log.info("Владелец бота: %s (%s)", user.id, user.username)
    if not _is_owner(update):
        return  # чужим молчим
    await update.message.reply_text("Привет! " + HELP, reply_markup=MENU)


def _lesson_buttons(lesson: dict) -> InlineKeyboardMarkup:
    lid = lesson["id"]
    rows = [[InlineKeyboardButton("📄 Конспект PDF", callback_data=f"pdf:{lid}")]]
    if lesson.get("shpora"):
        rows.append([InlineKeyboardButton("📝 Шпора A6", callback_data=f"sh6:{lid}"),
                     InlineKeyboardButton("📝 Шпора A7", callback_data=f"sh7:{lid}")])
    extra = []
    if botlogic.parse_quiz(lesson.get("quiz", "")):
        extra.append(InlineKeyboardButton("✅ Тест", callback_data=f"quiz:{lid}"))
    if lesson.get("videos"):
        extra.append(InlineKeyboardButton("🎬 Видео", callback_data=f"vid:{lid}"))
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton("❓ Спросить по этому уроку", callback_data=f"askl:{lid}")])
    return InlineKeyboardMarkup(rows)


def _done_lessons(limit: int = 12) -> list[dict]:
    return [l for l in db.all_lessons() if l["konspekt"]][:limit]


async def notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lessons = _done_lessons()
    if not lessons:
        await update.message.reply_text("Готовых конспектов пока нет.")
        return
    kb = [[InlineKeyboardButton(botlogic.lesson_title(l), callback_data=f"lesson:{l['id']}")] for l in lessons]
    await update.message.reply_text("Выбери урок:", reply_markup=InlineKeyboardMarkup(kb))


async def today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    day = datetime.now().date().isoformat()
    items = [l for l in db.all_lessons() if (l["starts_at"] or l["created_at"]).startswith(day)]
    if not items:
        await update.message.reply_text("На сегодня уроков в списке нет.")
        return
    lines = [f"• {(l['starts_at'] or '')[11:16]} {botlogic.lesson_title(l)} — "
             f"{db.STATUSES.get(l['status'], l['status'])}" for l in sorted(items, key=lambda l: l["starts_at"] or "")]
    await update.message.reply_text("Сегодня:\n" + "\n".join(lines))


async def ask_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lessons = _done_lessons(1)
    ctx.user_data["ask_lesson"] = lessons[0]["id"] if lessons else None
    about = f" (по уроку «{botlogic.lesson_title(lessons[0])}»)" if lessons else ""
    await update.message.reply_text(
        f"Напиши вопрос{about}. Другой урок можно выбрать в {BTN_NOTES} → «Спросить по этому уроку».")


async def add_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли файл урока: PDF-презентацию, аудио, голосовое или видео (до 20 МБ — ограничение Telegram). "
        "В подписи напиши «Предмет. Тема», например: «Биология. Осмос».")


async def text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    text = update.message.text.strip()
    routes = {BTN_NOTES: notes, BTN_TODAY: today, BTN_ASK: ask_button, BTN_ADD: add_button}
    if text in routes:
        await routes[text](update, ctx)
        return
    await _answer_question(update, ctx, text)


async def _answer_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    lesson_id = ctx.user_data.get("ask_lesson")
    if lesson_id is None:
        lessons = _done_lessons(1)
        lesson_id = lessons[0]["id"] if lessons else None
    lesson = db.get(lesson_id) if lesson_id else None
    qid = db.add_question(lesson_id, question, status="draft")
    claude_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Спросить Claude", callback_data=f"claude:{qid}")]])
    if not lesson:
        await update.message.reply_text("Пока нет материалов, по которым можно ответить.", reply_markup=claude_btn)
        return
    if not config.HF_TOKEN:
        await update.message.reply_text("Быстрые ответы не подключены (нет HF_TOKEN). Отправить вопрос Claude?",
                                        reply_markup=claude_btn)
        return
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(botlogic.ask_hf, question, botlogic.lesson_context(lesson))
    except Exception as e:
        log.warning("HF: %s", e)
        await update.message.reply_text("Быстрый ответ сейчас недоступен. Отправить вопрос Claude?",
                                        reply_markup=claude_btn)
        return
    for chunk in botlogic.md_to_telegram(f"⚡ {answer}"):
        await update.message.reply_text(chunk)
    await update.message.reply_text("Если ответ не помог — спроси Claude:", reply_markup=claude_btn)


async def file_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Файл урока из Telegram → новый урок в обработку."""
    if not _is_owner(update):
        return
    msg = update.message
    media = msg.document or msg.audio or msg.voice or msg.video or msg.video_note
    if media is None:
        return
    if getattr(media, "file_size", 0) and media.file_size > 20 * 1024 * 1024:
        await msg.reply_text("Файл больше 20 МБ — Telegram не даст его скачать. Загрузи его на сайте.")
        return
    caption = (msg.caption or "").strip()
    subject, _, title = caption.partition(".")
    name = getattr(media, "file_name", None) or ("voice.ogg" if msg.voice else "video.mp4" if msg.video else "audio.mp3")
    is_pdf = name.lower().endswith(".pdf") or getattr(media, "mime_type", "") == "application/pdf"
    lesson_id = db.create(subject=subject.strip() or "Урок", title=title.strip(), status="queued")
    folder = config.lesson_dir(lesson_id)
    target = folder / ("materials.pdf" if is_pdf else f"upload{Path(name).suffix.lower() or '.bin'}")
    tg_file = await media.get_file()
    await tg_file.download_to_drive(custom_path=str(target))
    db.log(lesson_id, f"Файл из Telegram: {name}")
    pipeline.enqueue(lesson_id)
    await msg.reply_text(f"Принято: {botlogic.lesson_title(db.get(lesson_id))}. Пришлю конспект, когда будет готов.")


async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_owner(update):
        await query.answer()
        return
    await query.answer()
    action, _, arg = query.data.partition(":")
    chat_id = query.message.chat_id
    if action == "claude":
        q = next((q for q in db.questions("draft") if q["id"] == int(arg)), None)
        if q:
            db.set_question_status(q["id"], "new")
            await query.message.reply_text("🧠 Вопрос отправлен Claude. Ответ придёт сюда.")
        return
    lesson = db.get(int(arg))
    if not lesson:
        await query.message.reply_text("Урок не найден.")
        return
    if action == "lesson":
        await query.message.reply_text(botlogic.lesson_title(lesson), reply_markup=_lesson_buttons(lesson))
    elif action in ("pdf", "sh6", "sh7"):
        await send_pdf(ctx.bot, chat_id, lesson, {"pdf": "A5", "sh6": "A6", "sh7": "A7"}[action])
    elif action == "quiz":
        await send_quiz(ctx.bot, chat_id, lesson)
    elif action == "vid":
        for chunk in botlogic.md_to_telegram("🎬 Видео по теме\n\n" + lesson["videos"]):
            await ctx.bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    elif action == "askl":
        ctx.user_data["ask_lesson"] = lesson["id"]
        await query.message.reply_text(f"Напиши вопрос по уроку «{botlogic.lesson_title(lesson)}».")


async def send_pdf(bot, chat_id: int, lesson: dict, fmt: str) -> None:
    text = lesson["konspekt"] if fmt == "A5" else lesson["shpora"]
    if not text:
        return
    kind = "конспект" if fmt == "A5" else f"шпора {fmt}"
    out = config.lesson_dir(lesson["id"]) / f"tg_{fmt}.pdf"
    await printing.build_print_pdf(text, out, fmt)
    name = f"{lesson['subject']} {lesson['title']} — {kind}.pdf".replace("/", "-").strip()
    with open(out, "rb") as f:
        await bot.send_document(chat_id, document=f, filename=name)


async def send_quiz(bot, chat_id: int, lesson: dict) -> None:
    polls = botlogic.parse_quiz(lesson.get("quiz", ""))
    if not polls:
        await bot.send_message(chat_id, "Теста к этому уроку пока нет.")
        return
    await bot.send_message(chat_id, f"✅ Тест: {botlogic.lesson_title(lesson)} — {len(polls)} вопр.")
    for p in polls:
        await bot.send_poll(chat_id, p["question"], p["options"], is_anonymous=False, type=Poll.QUIZ,
                            correct_option_id=p["correct"], explanation=p["explanation"] or None)


async def notify_lesson(bot, lesson_id: int) -> None:
    """Готовый урок → владельцу: конспект, шпоры, тест, видео."""
    chat_id = owner_id()
    lesson = db.get(lesson_id)
    if not chat_id or not lesson or not lesson["konspekt"] or lesson["notified"]:
        return
    db.update(lesson_id, notified=1)
    await bot.send_message(chat_id, f"📚 Готов конспект: {botlogic.lesson_title(lesson)}",
                           reply_markup=_lesson_buttons(lesson))
    await send_pdf(bot, chat_id, lesson, "A5")
    if lesson["shpora"]:
        await send_pdf(bot, chat_id, lesson, "A7")


async def notify_answer(bot, question: dict) -> None:
    chat_id = owner_id()
    if not chat_id:
        return
    for chunk in botlogic.md_to_telegram(f"🧠 Ответ Claude на вопрос «{question['text'][:200]}»:\n\n{question['answer']}"):
        await bot.send_message(chat_id, chunk)


def on_lesson_done(lesson_id: int) -> None:
    """Вызывается из обработки, когда конспект готов (в том же цикле событий)."""
    if _app is not None:
        asyncio.get_running_loop().create_task(_safe(notify_lesson(_app.bot, lesson_id)))


def on_answer(question: dict) -> None:
    if _app is not None:
        asyncio.get_running_loop().create_task(_safe(notify_answer(_app.bot, question)))


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("Ошибка отправки в Telegram")


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    owner = filters.User(user_id=owner_id()) if owner_id() else filters.ALL
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner, text_message))
    app.add_handler(MessageHandler((filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO) & owner,
                                   file_message))
    app.add_handler(CallbackQueryHandler(callback))
    return app


async def start_bot() -> Application | None:
    global _app
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    _app = build_application(config.TELEGRAM_BOT_TOKEN)
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram-бот запущен")
    return _app


async def stop_bot() -> None:
    global _app
    if _app is None:
        return
    await _app.updater.stop()
    await _app.stop()
    await _app.shutdown()
    _app = None
