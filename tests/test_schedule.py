"""Плавающий график и повтор раз в неделю. Запуск: python3 -m tests.test_schedule"""
import os
import subprocess
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="studytool-sched-"))
os.environ.update(DATA_DIR=str(TMP / "data"), APP_PASSWORD="", SUMMARY_MODE="api")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, pipeline, recorder  # noqa: E402


def test_form_and_weekly_repeat():
    with TestClient(main.app) as client:
        r = client.post("/lessons", data={
            "subject": "Английский", "webinar_url": "https://example.org/room",
            "start_mode": "at", "starts_at": "2099-10-16T17:30", "wait_minutes": "60", "repeat_weekly": "true",
        }, follow_redirects=False)
        assert r.status_code in (302, 303), r.text
        lesson = db.all_lessons()[0]
        assert lesson["wait_minutes"] == 60 and lesson["repeat_weekly"] == 1
        assert "до 60 мин" in client.get(f"/lessons/{lesson['id']}").text

    pipeline._schedule_next_week(db.get(lesson["id"]))
    nxt = [l for l in db.all_lessons() if l["id"] != lesson["id"]][0]
    assert nxt["starts_at"] == "2099-10-23T17:30:00" and nxt["status"] == "scheduled"
    assert nxt["wait_minutes"] == 60 and nxt["repeat_weekly"] == 1
    assert db.get(lesson["id"])["repeat_weekly"] == 0  # повтор переехал на следующую неделю, дублей нет


def test_trim_leading():
    src = TMP / "a.webm"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                    "-c:a", "libopus", str(src)], check=True)
    rec = recorder.Recording(db.create(subject="t", status="recording"))
    out = recorder.trim_leading(src, 60, rec)
    left = recorder_duration(out)
    assert 25 <= left <= 35, left


def recorder_duration(path):
    from app import transcribe
    return transcribe.duration(path)


if __name__ == "__main__":
    test_form_and_weekly_repeat()
    test_trim_leading()
    print("OK: плавающий график и повтор")
