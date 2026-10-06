"""Сквозная проверка веб-приложения (распознавание и Claude подменены заглушками — ключи не нужны).

Запуск: CHROMIUM_PATH=/opt/pw-browsers/chromium python3 -m tests.test_app
"""
import os
import subprocess
import tempfile
import time

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="studytool-app-"))
os.environ["APP_PASSWORD"] = "secret"

from fastapi.testclient import TestClient  # noqa: E402

from app import main, pipeline  # noqa: E402
from tests.test_recorder import serve  # noqa: E402

FAKE_KONSPEKT = """# Тест. Проверка

**10 класс**

## ⭐ Главная мысль
Всё работает.

| A | B |
|---|---|
| 1 | 2 |
"""
seen = {}


def fake_transcribe(audio, workdir, progress):
    seen["audio"] = audio
    progress("заглушка распознавания")
    return "[00:01] Тестовая речь учителя."


def fake_konspekt(lesson, transcript, pdf):
    seen["transcript"], seen["pdf"] = transcript, pdf
    return FAKE_KONSPEKT


pipeline.transcribe.transcribe = fake_transcribe
pipeline.summarize.build_konspekt = fake_konspekt


def wait_status(client, lesson_id, wanted, timeout):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/lessons/{lesson_id}/status").json()
        if s["status"] in wanted:
            return s
        time.sleep(1)
    raise AssertionError(f"не дождались {wanted}: {s}")


def main_test():
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"student:secret").decode()}
    with TestClient(main.app) as anon:
        assert anon.get("/").status_code == 401, "без пароля пускать не должно"
    with TestClient(main.app, headers=auth) as client:
        assert client.get("/").status_code == 200

        # 1) Загруженный файл записи
        wav = os.path.join(os.environ["DATA_DIR"], "t.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=300:d=3", wav], check=True)
        with open(wav, "rb") as f:
            r = client.post("/lessons", data={"subject": "Биология", "title": "Белки"},
                            files={"audio_file": ("urok.wav", f, "audio/wav")}, follow_redirects=False)
        assert r.status_code == 303, r.text
        lid = int(r.headers["location"].rsplit("/", 1)[1])
        s = wait_status(client, lid, {"done", "error"}, 30)
        assert s["status"] == "done", s
        page = client.get(f"/lessons/{lid}").text
        assert "Главная мысль" in page and "<table>" in page
        pdf = client.get(f"/lessons/{lid}/print.pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF"), pdf.status_code
        assert client.get(f"/lessons/{lid}/konspekt.md").text == FAKE_KONSPEKT
        client.post(f"/lessons/{lid}/edit", data={"konspekt": "# Правка"})
        assert "Правка" in client.get(f"/lessons/{lid}").text
        print("upload flow OK, PDF", len(pdf.content), "bytes")

        # 2) Вебинар: бот по расписанию заходит в имитацию MTS Link
        port = serve()
        r = client.post("/lessons", data={"subject": "История", "start_mode": "now",
                                          "webinar_url": f"http://127.0.0.1:{port}/index.html?ends=20"},
                        follow_redirects=False)
        lid2 = int(r.headers["location"].rsplit("/", 1)[1])
        wait_status(client, lid2, {"recording"}, 60)
        assert client.get(f"/lessons/{lid2}/bot.png").status_code == 200
        s = wait_status(client, lid2, {"done", "error"}, 90)
        assert s["status"] == "done", s
        assert str(seen["audio"]).endswith("audio.webm")
        print(s["log"])
        print("webinar flow OK")

        # 3) Только материалы, без записи (PDF-файл)
        r = client.post("/lessons", data={"subject": "История"},
                        files={"materials_file": ("m.pdf", pdf.content, "application/pdf")}, follow_redirects=False)
        lid3 = int(r.headers["location"].rsplit("/", 1)[1])
        s = wait_status(client, lid3, {"done", "error"}, 30)
        assert s["status"] == "done" and seen["pdf"] and seen["transcript"] == "", s
        print("materials-only flow OK")

        # 4) Остановка записи вручную
        r = client.post("/lessons", data={"subject": "Физика", "start_mode": "now",
                                          "webinar_url": f"http://127.0.0.1:{port}/index.html?ends=600"},
                        follow_redirects=False)
        lid4 = int(r.headers["location"].rsplit("/", 1)[1])
        wait_status(client, lid4, {"recording"}, 60)
        time.sleep(8)
        client.post(f"/lessons/{lid4}/stop")
        s = wait_status(client, lid4, {"done", "error"}, 60)
        assert s["status"] == "done" and "остановлено вручную" in s["log"], s
        print("manual stop OK")
    print("ALL OK")


if __name__ == "__main__":
    main_test()
