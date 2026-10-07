"""Режим без API: сервер кладёт урок в репозиторий, «Claude» дописывает konspekt.md, сервер его забирает.

Вместо GitHub — локальный репозиторий. Запуск: python3 -m tests.test_sync
"""
import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="studytool-sync-"))
REMOTE = TMP / "remote.git"
subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(REMOTE)], check=True)
seed = TMP / "seed"
subprocess.run(["git", "clone", "-q", str(REMOTE), str(seed)], check=True)
(seed / "README.md").write_text("test\n")
for cmd in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], ["push", "-q", "origin", "HEAD:master"]):
    subprocess.run(["git", "-C", str(seed), *cmd], check=True)

os.environ.update(DATA_DIR=str(TMP / "data"), APP_PASSWORD="", SUMMARY_MODE="session",
                  GIT_REPO=str(REMOTE), GIT_BRANCH="master", SYNC_MINUTES="0.05")

from fastapi.testclient import TestClient  # noqa: E402

from app import main, pipeline  # noqa: E402

pipeline.transcribe.transcribe = lambda audio, workdir, progress: "[00:05] Сегодня тема — осмос."


def wait(client, lid, wanted, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/lessons/{lid}/status").json()
        if s["status"] in wanted:
            return s
        time.sleep(0.5)
    raise AssertionError(s)


def main_test():
    with TestClient(main.app) as client:
        wav = TMP / "t.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2", str(wav)], check=True)
        r = client.post("/lessons", data={"subject": "Биология", "title": "Осмос", "notes": "повторить осмос"},
                        files={"audio_file": ("a.wav", wav.read_bytes(), "audio/wav"),
                               "materials_file": ("m.pdf", b"%PDF-1.4 test", "application/pdf")},
                        follow_redirects=False)
        lid = int(r.headers["location"].rsplit("/", 1)[1])
        s = wait(client, lid, {"waiting", "error"})
        assert s["status"] == "waiting", s
        assert "Ждёт конспекта" in client.get(f"/lessons/{lid}").text

        # «Claude» забирает урок из репозитория и пишет конспект
        work = TMP / "claude"
        subprocess.run(["git", "clone", "-q", str(REMOTE), str(work)], check=True)
        folders = list((work / "lessons").glob("*"))
        assert len(folders) == 1, folders
        f = folders[0]
        meta = json.loads((f / "meta.json").read_text())
        assert meta["server_lesson_id"] == lid and meta["notes"] == "повторить осмос", meta
        assert "осмос" in (f / "transcript.txt").read_text() and (f / "materials.pdf").exists()
        (f / "konspekt.md").write_text("# Биология. Осмос\n\n## ⭐ Главная мысль\nВода идёт туда, где солонее.\n")
        for cmd in (["add", "."], ["-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "Конспект"], ["push", "-q", "origin", "HEAD:master"]):
            subprocess.run(["git", "-C", str(work), *cmd], check=True)

        s = wait(client, lid, {"done"})
        page = client.get(f"/lessons/{lid}").text
        assert "Вода идёт туда, где солонее" in page
        assert client.get(f"/lessons/{lid}/print.pdf").content.startswith(b"%PDF")
        print("session flow OK:", folders[0].name)

        # Ручной путь с телефона: вставить конспект в форму
        r = client.post("/lessons", data={"subject": "История"},
                        files={"audio_file": ("b.wav", wav.read_bytes(), "audio/wav")}, follow_redirects=False)
        lid2 = int(r.headers["location"].rsplit("/", 1)[1])
        wait(client, lid2, {"waiting"})
        client.post(f"/lessons/{lid2}/edit", data={"konspekt": "# История\n\nТекст"})
        assert client.get(f"/lessons/{lid2}/status").json()["status"] == "done"
        print("manual paste OK")
    print("ALL OK")


if __name__ == "__main__":
    main_test()
