"""Проверка бота на имитации вебинара: вход по имени, запись WebRTC-звука, остановка по «Мероприятие завершено».

Запуск: CHROMIUM_PATH=/opt/pw-browsers/chromium python3 -m tests.test_recorder
"""
import asyncio
import functools
import http.server
import os
import subprocess
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="studytool-test-"))
os.environ.setdefault("BOT_NAME", "Тест Бот")

from app import db, recorder, transcribe  # noqa: E402

HERE = Path(__file__).parent / "fake_webinar"


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def main() -> None:
    port = serve()
    db.init()
    lesson_id = db.create(subject="Тест", webinar_url=f"http://127.0.0.1:{port}/index.html?ends=25", status="scheduled")
    audio = asyncio.run(recorder.record(lesson_id))
    lesson = db.get(lesson_id)
    print(lesson["log"])
    assert audio and audio.exists(), "аудио не записано"
    dur = transcribe.duration(audio)
    vol = subprocess.run(["ffmpeg", "-i", str(audio), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    mean = [l for l in vol.splitlines() if "mean_volume" in l]
    print(f"audio: {audio} {audio.stat().st_size} bytes, {dur:.1f}s, {mean}")
    assert dur > 10, f"слишком короткая запись: {dur}"
    assert "Ввёл имя" in lesson["log"] and "Нажал «Войти»" in lesson["log"], "бот не прошёл форму входа"
    assert "эфир завершён" in lesson["log"], "бот не заметил конец эфира"
    print("OK")


if __name__ == "__main__":
    main()
