"""Настройки приложения из переменных окружения (см. .env.example)."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "studytool.sqlite3"

# Вход в веб-интерфейс (HTTP Basic). Без пароля приложение не стартует на сервере.
APP_USER = os.environ.get("APP_USER", "student")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Имя, под которым бот заходит в вебинар.
BOT_NAME = os.environ.get("BOT_NAME", "Ученик")
# Сколько максимум длится запись, если эфир сам не закончился (минуты).
MAX_RECORD_MINUTES = int(os.environ.get("MAX_RECORD_MINUTES", "120"))
# Через сколько минут тишины (нет звука с вебинара) считать эфир законченным.
SILENCE_STOP_MINUTES = int(os.environ.get("SILENCE_STOP_MINUTES", "10"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH") or None

# Распознавание речи: "api" (OpenAI-совместимый Whisper API: OpenAI, Groq и т.п.) или "local" (faster-whisper).
TRANSCRIBE_PROVIDER = os.environ.get("TRANSCRIBE_PROVIDER", "api")
TRANSCRIBE_API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "")
TRANSCRIBE_BASE_URL = os.environ.get("TRANSCRIBE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")
LOCAL_WHISPER_MODEL = os.environ.get("LOCAL_WHISPER_MODEL", "large-v3-turbo")

# Конспекты: Claude API (ключ в ANTHROPIC_API_KEY).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "high")

TIMEZONE = os.environ.get("TZ", "Europe/Moscow")


def lesson_dir(lesson_id: int) -> Path:
    d = DATA_DIR / "lessons" / str(lesson_id)
    d.mkdir(parents=True, exist_ok=True)
    return d
