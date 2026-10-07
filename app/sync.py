"""Обмен с репозиторием на GitHub: сервер выкладывает расшифровки уроков в lessons/,
Claude (в обычной подписке, без API) пишет рядом konspekt.md, сервер забирает готовые конспекты.
"""
import json
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import config

REPO_DIR = config.DATA_DIR / "repo"
_lock = threading.Lock()


def enabled() -> bool:
    return bool(config.GIT_REPO and (config.GITHUB_TOKEN or config.GIT_REPO.startswith(("/", "file://"))))


def _url() -> str:
    if config.GIT_REPO.startswith(("/", "file://")):  # локальный репозиторий — для тестов
        return config.GIT_REPO
    repo = config.GIT_REPO.removeprefix("https://").removeprefix("github.com/").removesuffix(".git")
    return f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{repo}.git"


def _redact(text: str) -> str:
    """Токен не должен попасть в журнал."""
    return text.replace(config.GITHUB_TOKEN, "***") if config.GITHUB_TOKEN else text


def _git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {_redact(res.stderr or res.stdout)[:300]}")
    return res.stdout


def _ensure_clone() -> None:
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["git", "clone", "-q", "--depth", "50", "-b", config.GIT_BRANCH, _url(), str(REPO_DIR)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError("Не удалось скачать репозиторий: " + _redact(res.stderr)[:300])
        _git("config", "user.name", "StudyTool server")
        _git("config", "user.email", "studytool@localhost")
    else:
        _git("remote", "set-url", "origin", _url())


def _pull() -> None:
    _git("fetch", "-q", "origin", config.GIT_BRANCH)
    _git("reset", "-q", "--hard", f"origin/{config.GIT_BRANCH}")


def folder_name(lesson: dict) -> str:
    date = (lesson.get("starts_at") or lesson["created_at"])[:10]
    slug = re.sub(r"[^\w]+", "_", f"{lesson['subject']} {lesson['title']}".strip().lower()).strip("_")[:50]
    return f"{date}_{lesson['id']:03d}_{slug}"


def publish(lesson: dict, transcript: str, materials: Path | None) -> str:
    """Кладёт урок в lessons/<папка>/ и отправляет на GitHub. Возвращает путь папки в репозитории."""
    rel = f"lessons/{folder_name(lesson)}"
    meta = {
        "server_lesson_id": lesson["id"],
        "subject": lesson["subject"],
        "title": lesson["title"],
        "teacher": lesson["teacher"],
        "date": (lesson.get("starts_at") or lesson["created_at"])[:10],
        "notes": lesson["notes"],
        "materials_url": lesson["materials_url"],
        "published_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        _ensure_clone()
        for attempt in range(3):
            _pull()
            folder = REPO_DIR / rel
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "transcript.txt").write_text(transcript or "(записи нет — только материалы)\n", encoding="utf-8")
            if materials and materials.exists():
                (folder / "materials.pdf").write_bytes(materials.read_bytes())
            _git("add", rel)
            if not _git("status", "--porcelain", rel).strip():
                return rel
            _git("commit", "-q", "-m", f"Урок: {lesson['subject']} {lesson['title']}".strip())
            try:
                _git("push", "-q", "origin", f"HEAD:{config.GIT_BRANCH}")
                return rel
            except RuntimeError:
                if attempt == 2:
                    raise
    return rel


def collect_konspekts() -> dict[int, str]:
    """Свежие konspekt.md из репозитория: {id урока на сервере: текст}."""
    found: dict[int, str] = {}
    with _lock:
        _ensure_clone()
        _pull()
        for meta_path in (REPO_DIR / "lessons").glob("*/meta.json"):
            konspekt = meta_path.parent / "konspekt.md"
            if not konspekt.exists():
                continue
            try:
                lesson_id = int(json.loads(meta_path.read_text(encoding="utf-8"))["server_lesson_id"])
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            found[lesson_id] = konspekt.read_text(encoding="utf-8")
    return found
