"""Конспект урока через Claude API: расшифровка + материалы (PDF) → Markdown."""
import base64
from datetime import datetime

import anthropic
import httpx

from . import config

SYSTEM_PROMPT = """Ты составляешь учебный конспект школьного урока (10 класс) для подготовки к контрольным и тестам.

Источники: расшифровка речи учителя с таймкодами [мм:сс] и/или материалы урока (презентация). Расшифровка сделана автоматически: в ней бывают ошибки распознавания — исправляй очевидные по смыслу (термины, имена, даты), а действительно непонятные места помечай «(неразборчиво)».

Требования к конспекту:
- Сохрани ВСЕ содержательные детали урока: определения, даты, имена, формулы, примеры, причины и следствия, выводы. Особо отметь, что учитель назвал важным, «будет на контрольной», что задал выучить или сделать.
- Не выдумывай то, чего не было на уроке. Всё, что ты добавляешь сверх урока, — только в раздел «💡 Дополнительно».
- Пиши так, будто конспект составил сам ученик: нигде не упоминай ИИ, нейросети, модели, «расшифровку», «распознавание», «автоматически». Никаких обращений к читателю о том, как конспект сделан.
- Язык — русский. Для иностранного языка: объяснения по-русски, примеры на изучаемом языке.
- Сравнения, хронологии, классификации оформляй таблицами Markdown. Ключевые термины выделяй **жирным**.
- В заголовках разделов, которые взяты из речи учителя, указывай таймкод начала в скобках, например «## 2. Пептидная связь (14:20)».

Структура (строго в этом порядке):
# <Предмет>. <Тема урока>
**<класс · преподаватель · дата — что известно>**

---

## ⭐ Главная мысль
2–4 предложения: о чём урок и как связаны его части.

## 1. … / ## 2. … — подробный конспект по ходу урока (разделы и подразделы).

## 📝 Задано
Домашнее задание и что сказали выучить/повторить (раздел только если это прозвучало или указано в заметках).

## 💡 Дополнительно
Полезные факты, пояснения, мнемоники, типичные ловушки в тестах — то, чего не было на уроке, но поможет подготовиться.

## ✅ Вопросы для самопроверки
8–12 вопросов по материалу урока.

Выведи только сам конспект в Markdown, без вступлений и пояснений."""

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def fetch_pdf(url: str) -> bytes:
    resp = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=120)
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise RuntimeError("По ссылке на материалы пришёл не PDF")
    return resp.content


def build_konspekt(lesson: dict, transcript: str, pdf: bytes | None) -> str:
    meta = [f"Предмет: {lesson['subject']}"]
    if lesson.get("title"):
        meta.append(f"Тема: {lesson['title']}")
    if lesson.get("teacher"):
        meta.append(f"Преподаватель: {lesson['teacher']}")
    date = (lesson.get("starts_at") or lesson.get("created_at") or "")[:10]
    if date:
        meta.append(f"Дата: {datetime.fromisoformat(date).strftime('%d.%m.%Y')}")
    if lesson.get("notes"):
        meta.append(f"Заметки ученика (что задали, на что обратить внимание): {lesson['notes']}")

    content: list[dict] = []
    if pdf:
        content.append({"type": "document", "title": "Материалы урока",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.standard_b64encode(pdf).decode()}})
    text = "\n".join(meta) + "\n\n"
    if transcript:
        text += f"<transcript>\n{transcript}\n</transcript>\n\nСоставь конспект урока."
    else:
        text += "Записи урока нет — составь конспект по материалам урока (приложены)."
    content.append({"type": "text", "text": text})

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=config.CLAUDE_MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": config.CLAUDE_EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude отказался составлять конспект по этому материалу")
    result = "".join(b.text for b in message.content if b.type == "text").strip()
    if not result:
        raise RuntimeError(f"Пустой ответ от Claude (stop_reason={message.stop_reason})")
    if message.stop_reason == "max_tokens":
        result += "\n\n*(конспект обрезан — слишком длинный урок)*"
    return result
