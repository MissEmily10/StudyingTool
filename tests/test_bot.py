"""Проверка бота без настоящего Telegram: викторины, рассылка готового урока, вопросы к Claude через git.

Запуск: CHROMIUM_PATH=/opt/pw-browsers/chromium python3 -m tests.test_bot
"""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="studytool-bot-"))
REMOTE = TMP / "remote.git"
subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(REMOTE)], check=True)
seed = TMP / "seed"
subprocess.run(["git", "clone", "-q", str(REMOTE), str(seed)], check=True, capture_output=True)
(seed / "README.md").write_text("test\n")
for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
            ["push", "-q", "origin", "HEAD:master"]):
    subprocess.run(["git", "-C", str(seed), *cmd], check=True, capture_output=True)

os.environ.update(DATA_DIR=str(TMP / "data"), SUMMARY_MODE="session", GIT_REPO=str(REMOTE), GIT_BRANCH="master")

from app import bot, botlogic, db, pipeline, sync  # noqa: E402

QUIZ = json.dumps([
    {"question": "Куда идёт вода при осмосе?", "options": ["Туда, где солонее", "Туда, где пресней"],
     "correct": 0, "explanation": "Вода идёт к большей концентрации."},
    {"question": "Плохой вопрос", "options": ["один"], "correct": 0},            # 1 вариант — пропустить
    {"question": "Плохой номер", "options": ["а", "б"], "correct": 5},          # нет такого варианта
    {"question": "x" * 400, "options": ["a" * 150, "b"], "correct": 1, "explanation": "y" * 300},  # обрезать
], ensure_ascii=False)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("message", text, kw))

    async def send_document(self, chat_id, document, filename=None, **kw):
        self.sent.append(("document", filename, document.read()[:4]))

    async def send_poll(self, chat_id, question, options, **kw):
        self.sent.append(("poll", question, options, kw))


def git_as_claude(*changes):
    """Клонировать репозиторий, применить изменения, запушить — как будто это сделал Claude."""
    work = TMP / f"claude_{len(list(TMP.glob('claude_*')))}"
    subprocess.run(["git", "clone", "-q", str(REMOTE), str(work)], check=True, capture_output=True)
    for fn in changes:
        fn(work)
    if not changes:
        return work  # только посмотреть, что лежит в репозитории
    for cmd in (["add", "."], ["-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "claude"],
                ["push", "-q", "origin", "HEAD:master"]):
        subprocess.run(["git", "-C", str(work), *cmd], check=True, capture_output=True)
    return work


async def main():
    # --- викторины
    polls = botlogic.parse_quiz(QUIZ)
    assert len(polls) == 2, polls
    assert len(polls[1]["question"]) <= 300 and len(polls[1]["options"][0]) <= 100 and len(polls[1]["explanation"]) <= 200
    assert botlogic.parse_quiz("не json") == [] and botlogic.parse_quiz("") == []
    assert len(botlogic.md_to_telegram("абзац\n\n" * 3000)) > 1
    print("quiz parsing OK")

    db.init()
    db.set_setting("telegram_owner", "42")
    fake = FakeBot()
    bot._app = type("A", (), {"bot": fake})()  # вместо запущенного Telegram-приложения

    # --- урок ждёт конспекта → «Claude» пишет конспект, шпору, тест, видео → бот присылает
    lid = db.create(subject="Биология", title="Осмос", status="queued", transcript="[00:01] Осмос — это…")
    pipeline._hand_over(lid, "[00:01] Осмос — это…", None)
    assert db.get(lid)["status"] == "waiting"
    folder = f"lessons/{sync.folder_name(db.get(lid))}"

    def write_lesson(w):
        d = w / folder
        (d / "konspekt.md").write_text("# Биология. Осмос\n\n## ⭐ Главная мысль\nВода идёт туда, где солонее.\n")
        (d / "shpora.md").write_text("# Осмос — шпора\n\n**Осмос** → вода к большей концентрации\n")
        (d / "quiz.json").write_text(QUIZ)
        (d / "videos.md").write_text("- [Осмос за 5 минут](https://www.youtube.com/watch?v=test) — канал, 5 мин; наглядно.\n")
    git_as_claude(write_lesson)

    await pipeline.sync_once()
    await asyncio.sleep(0.5)  # уведомление уходит отдельной задачей
    for _ in range(60):
        if sum(1 for s in fake.sent if s[0] == "document") >= 2:
            break
        await asyncio.sleep(0.5)
    lesson = db.get(lid)
    assert lesson["status"] == "done" and lesson["quiz"] and lesson["videos"] and lesson["notified"] == 1, lesson
    kinds = [s[0] for s in fake.sent]
    assert kinds[0] == "message" and "Готов конспект" in fake.sent[0][1], fake.sent[0]
    docs = [s for s in fake.sent if s[0] == "document"]
    assert len(docs) == 2 and all(d[2] == b"%PDF" for d in docs), docs
    print("lesson notification OK:", [d[1] for d in docs])

    # повторно не присылаем
    before = len(fake.sent)
    await bot.notify_lesson(fake, lid)
    assert len(fake.sent) == before

    await bot.send_quiz(fake, 42, db.get(lid))
    sent_polls = [s for s in fake.sent if s[0] == "poll"]
    assert len(sent_polls) == 2 and sent_polls[0][3]["correct_option_id"] == 0
    print("quiz sending OK")

    # --- вопрос к Claude: черновик → кнопка → в репозиторий → ответ → в Telegram
    qid = db.add_question(lid, "Почему эритроцит лопается в воде?", status="draft")
    db.set_question_status(qid, "new")  # как будто нажали «Спросить Claude»
    await pipeline.sync_once()
    assert db.questions("asked")[0]["id"] == qid
    work = git_as_claude()
    qfile = work / "questions" / f"{qid:04d}.json"
    data = json.loads(qfile.read_text())
    assert data["lesson_folder"] == folder and "эритроцит" in data["question"], data
    git_as_claude(lambda w: (w / "questions" / f"{qid:04d}.answer.md").write_text("Потому что вода входит в клетку."))
    await pipeline.sync_once()
    await asyncio.sleep(0.5)
    assert db.questions("answered")[0]["answer"].startswith("Потому что")
    assert any(s[0] == "message" and "Ответ Claude" in s[1] for s in fake.sent)
    print("claude question flow OK")
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
