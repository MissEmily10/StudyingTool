"""Бот, который заходит в вебинар MTS Link как участник и записывает звук.

Звук перехватывается внутри страницы (см. capture.js), поэтому серверу не нужны
звуковая карта и PulseAudio. На каждом шаге бот сохраняет скриншот bot.png —
по нему видно, что происходит, и легко поправить вход, если MTS Link поменяет интерфейс.
"""
import asyncio
import base64
import re
import time
from pathlib import Path

from playwright.async_api import Frame, Page, async_playwright

from . import config, db

CAPTURE_JS = (Path(__file__).parent / "capture.js").read_text(encoding="utf-8")

# Кнопки, которые бот нажимает по пути в комнату (регистр не важен).
JOIN_BUTTONS = re.compile(
    r"^\s*(войти|присоединиться|продолжить|принять участие|войти в мероприятие|войти как гость|"
    r"перейти к мероприятию|войти без (камеры|микрофона|устройств)|продолжить без (камеры|микрофона|устройств)|"
    r"включить звук|понятно|хорошо|ок|ok|согласен|принять|разрешить звук|смотреть)\s*$",
    re.I,
)
NAME_INPUT = (
    "input[name='name'], input[name='nickname'], input[name='firstName'], "
    "input[placeholder*='Имя' i], input[placeholder*='имя' i], input[placeholder*='name' i], "
    "input[autocomplete='name'], input[autocomplete='given-name']"
)
SURNAME_INPUT = "input[name='lastName'], input[name='secondName'], input[placeholder*='Фамили' i]"
ENDED_TEXT = re.compile(
    r"(мероприятие|вебинар|трансляция|встреча|эфир)\s+(завершен|завершён|окончен|закончил)", re.I
)


class Recording:
    def __init__(self, lesson_id: int):
        self.lesson_id = lesson_id
        self.dir = config.lesson_dir(lesson_id)
        self.chunks: dict[str, Path] = {}  # кадр (frame) → файл с его записью
        self.bytes = 0

    def log(self, msg: str) -> None:
        db.log(self.lesson_id, msg)

    def on_chunk(self, source: dict, b64: str) -> None:
        frame = source.get("frame")
        key = str(id(frame))
        path = self.chunks.setdefault(key, self.dir / f"capture_{len(self.chunks)}.webm")
        data = base64.b64decode(b64)
        with open(path, "ab") as f:
            f.write(data)
        self.bytes += len(data)

    def finalize(self) -> Path | None:
        """Оставляем запись из того кадра страницы, где было больше всего звука."""
        files = [p for p in self.chunks.values() if p.exists() and p.stat().st_size > 0]
        if not files:
            return None
        best = max(files, key=lambda p: p.stat().st_size)
        target = self.dir / "audio.webm"
        best.replace(target)
        for p in files:
            if p != best:
                p.unlink(missing_ok=True)
        return target


async def screenshot(page: Page, rec: Recording) -> None:
    try:
        await page.screenshot(path=str(rec.dir / "bot.png"))
    except Exception:
        pass


async def try_join_step(page: Page, rec: Recording, clicked: dict[str, int]) -> None:
    """Один проход по форме входа: ввести имя, отметить согласия, нажать подходящую кнопку."""
    for frame in page.frames:
        await _join_in_frame(frame, rec, clicked)


async def _join_in_frame(frame: Frame, rec: Recording, clicked: dict[str, int]) -> None:
    try:
        name_inputs = frame.locator(NAME_INPUT)
        for i in range(await name_inputs.count()):
            inp = name_inputs.nth(i)
            if await inp.is_visible() and not (await inp.input_value()).strip():
                first, _, last = config.BOT_NAME.partition(" ")
                surname = frame.locator(SURNAME_INPUT)
                if last and await surname.count() and await surname.first.is_visible():
                    await inp.fill(first)
                    await surname.first.fill(last)
                else:
                    await inp.fill(config.BOT_NAME)
                rec.log(f"Ввёл имя «{config.BOT_NAME}»")

        boxes = frame.locator("input[type='checkbox']:not(:checked)")
        for i in range(await boxes.count()):
            box = boxes.nth(i)
            if await box.is_visible():
                await box.check(timeout=2000)
                rec.log("Отметил галочку (согласие)")

        buttons = frame.locator("button, [role='button'], a[class*='button' i], input[type='submit']")
        for i in range(min(await buttons.count(), 60)):
            btn = buttons.nth(i)
            if not await btn.is_visible():
                continue
            text = (await btn.inner_text(timeout=1000)).strip() if await btn.evaluate("e => e.tagName") != "INPUT" \
                else (await btn.get_attribute("value") or "")
            if text and JOIN_BUTTONS.match(text) and await btn.is_enabled():
                key = f"{frame.url}|{text}"
                if clicked.get(key, 0) >= 3:  # не жмём одну и ту же кнопку бесконечно
                    continue
                clicked[key] = clicked.get(key, 0) + 1
                await btn.click(timeout=3000)
                rec.log(f"Нажал «{text}»")
                await frame.page.wait_for_timeout(1500)
                return
    except Exception as e:  # кадры появляются и исчезают — это нормально
        if "detached" not in str(e).lower():
            rec.log(f"Шаг входа: {type(e).__name__}: {str(e)[:120]}")


async def page_text(page: Page) -> str:
    texts = []
    for frame in page.frames:
        try:
            texts.append(await frame.inner_text("body", timeout=2000))
        except Exception:
            pass
    return "\n".join(texts)


async def capture_state(page: Page) -> dict:
    """Суммарное состояние перехвата звука по всем кадрам страницы."""
    state = {"tracks": 0, "last_sound": 0, "started": False}
    for frame in page.frames:
        try:
            s = await frame.evaluate(
                "() => window.__stCapture ? {tracks: window.__stCapture.tracks.size,"
                " last: window.__stCapture.lastSound || 0, started: window.__stCapture.started} : null"
            )
        except Exception:
            continue
        if s:
            state["tracks"] += s["tracks"]
            state["last_sound"] = max(state["last_sound"], s["last"])
            state["started"] |= s["started"]
    return state


async def record(lesson_id: int) -> Path | None:
    lesson = db.get(lesson_id)
    rec = Recording(lesson_id)
    db.update(lesson_id, status="joining", error="")
    rec.log(f"Открываю {lesson['webinar_url']}")

    async with async_playwright() as p:
        launch = dict(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        if config.CHROMIUM_PATH:
            launch["executable_path"] = config.CHROMIUM_PATH
        else:
            launch["channel"] = "chromium"  # полноценный Chromium в новом headless-режиме: со звуком
        browser = await p.chromium.launch(**launch)
        context = await browser.new_context(
            locale="ru-RU", timezone_id=config.TIMEZONE, viewport={"width": 1366, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        )
        await context.expose_binding("__stCaptureChunk", rec.on_chunk)
        await context.add_init_script(CAPTURE_JS)
        page = await context.new_page()

        started = time.monotonic()
        recording_since: float | None = None
        last_shot = 0.0
        clicked: dict[str, int] = {}
        stop_reason = ""
        try:
            await page.goto(lesson["webinar_url"], timeout=90_000)
            while True:
                await page.wait_for_timeout(5000)
                now = time.monotonic()
                if now - last_shot > 20:
                    await screenshot(page, rec)
                    last_shot = now

                if db.get(lesson_id)["stop_requested"]:
                    stop_reason = "остановлено вручную"
                    break
                if now - started > config.MAX_RECORD_MINUTES * 60:
                    stop_reason = f"прошло {config.MAX_RECORD_MINUTES} мин (максимум)"
                    break

                state = await capture_state(page)
                if recording_since is None:
                    if state["tracks"] and rec.bytes > 0:
                        recording_since = now
                        db.update(lesson_id, status="recording")
                        rec.log("Звук пошёл — идёт запись")
                    else:
                        await try_join_step(page, rec, clicked)
                        continue

                if ENDED_TEXT.search(await page_text(page)):
                    stop_reason = "эфир завершён"
                    break
                silent_for = time.time() - state["last_sound"] / 1000 if state["last_sound"] else 0
                if silent_for > config.SILENCE_STOP_MINUTES * 60:
                    stop_reason = f"тишина {config.SILENCE_STOP_MINUTES} мин"
                    break
        except Exception as e:
            stop_reason = f"ошибка: {type(e).__name__}: {str(e)[:200]}"
        finally:
            await screenshot(page, rec)
            for frame in page.frames:
                try:
                    await frame.evaluate("() => window.__stCapture && window.__stCapture.stop && window.__stCapture.stop()")
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            await browser.close()

    audio = rec.finalize()
    mb = rec.bytes / 1_000_000
    rec.log(f"Запись остановлена: {stop_reason}. Записано {mb:.1f} МБ")
    return audio
