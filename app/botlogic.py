"""Логика бота без привязки к Telegram: тесты-викторины, видео, контекст для вопросов, ответ модели HF."""
import json
import re

import httpx

from . import config

# Ограничения Telegram для опросов-викторин
Q_MAX, OPT_MAX, EXPL_MAX = 300, 100, 200


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_quiz(raw: str) -> list[dict]:
    """quiz.json → список вопросов, годных для send_poll. Плохие вопросы пропускаем, а не падаем.

    Формат: [{"question": "...", "options": ["...", "..."], "correct": 0, "explanation": "..."}]
    """
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("questions", [])
    polls = []
    for item in data if isinstance(data, list) else []:
        try:
            options = [_cut(o, OPT_MAX) for o in item["options"]][:10]
            correct = int(item["correct"])
            if len(options) < 2 or not 0 <= correct < len(options) or len(set(options)) != len(options):
                continue
            polls.append({
                "question": _cut(item["question"], Q_MAX),
                "options": options,
                "correct": correct,
                "explanation": _cut(item.get("explanation", ""), EXPL_MAX),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return polls


def lesson_title(lesson: dict) -> str:
    title = lesson["subject"] + (f". {lesson['title']}" if lesson.get("title") else "")
    date = (lesson.get("starts_at") or lesson.get("created_at") or "")[:10]
    return f"{title} ({date[8:10]}.{date[5:7]})" if date else title


def lesson_context(lesson: dict, limit: int = 60_000) -> str:
    """Материалы урока для «быстрого вопроса»: конспект важнее всего, потом шпора, потом речь учителя."""
    parts = [f"Урок: {lesson_title(lesson)}"]
    for name, key in (("КОНСПЕКТ", "konspekt"), ("ШПОРА", "shpora"), ("РЕЧЬ УЧИТЕЛЯ", "transcript")):
        if lesson.get(key):
            parts.append(f"=== {name} ===\n{lesson[key]}")
    text = "\n\n".join(parts)
    return text[:limit]


ASK_SYSTEM = (
    "Ты помогаешь школьнице 10 класса разобраться в учебном материале. Отвечай по-русски, коротко и понятно, "
    "простыми словами, с примером, если он помогает. Опирайся ТОЛЬКО на материалы урока ниже. "
    "Если ответа в материалах нет — так и скажи одной фразой и посоветуй нажать «Спросить Claude». "
    "Не выдумывай факты, формулы и даты."
)


def ask_hf(question: str, context: str) -> str:
    """Быстрый ответ открытой модели (Hugging Face Inference Providers, OpenAI-совместимый чат)."""
    if not config.HF_TOKEN:
        raise RuntimeError("Не задан HF_TOKEN — быстрые ответы выключены")
    resp = httpx.post(
        f"{config.HF_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.HF_TOKEN}"},
        json={
            "model": config.HF_MODEL,
            "messages": [
                {"role": "system", "content": ASK_SYSTEM},
                {"role": "user", "content": f"МАТЕРИАЛЫ:\n{context}\n\nВОПРОС: {question}"},
            ],
            "max_tokens": 900,
            "temperature": 0.3,
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Hugging Face ответил {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def md_to_telegram(text: str, limit: int = 3900) -> list[str]:
    """Markdown → простой текст для Telegram, порезанный на сообщения (лимит Telegram 4096 символов)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [""]
