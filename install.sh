#!/usr/bin/env bash
# Установка StudyTool на чистый сервер Ubuntu одной командой:
#   curl -fsSL https://raw.githubusercontent.com/MissEmily10/StudyingTool/master/install.sh | bash   (от root)
# Повторный запуск обновляет приложение до последней версии (настройки в .env сохраняются).
set -euo pipefail

REPO=https://github.com/MissEmily10/StudyingTool.git
DIR=/opt/StudyingTool

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ask() {  # ask "Вопрос" ПЕРЕМЕННАЯ [по умолчанию] — читаем с терминала, даже если скрипт пришёл через curl | bash
  local prompt=$1 var=$2 def=${3:-} ans
  read -r -p "$prompt${def:+ [$def]}: " ans </dev/tty || true
  printf -v "$var" '%s' "${ans:-$def}"
}

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo"; exit 1; }

say "1/5 Устанавливаю Docker и git"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }

say "2/5 Скачиваю приложение в $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
elif ! git clone -q "$REPO" "$DIR" 2>/dev/null; then
  # репозиторий закрытый — нужен токен
  ask "Репозиторий закрытый. Токен GitHub (github_pat_...)" GITHUB_TOKEN ""
  git clone -q "https://x-access-token:${GITHUB_TOKEN}@github.com/MissEmily10/StudyingTool.git" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  say "3/5 Настройки (их можно поменять позже: nano $DIR/.env)"
  ask "Имя, под которым бот заходит на вебинар (его увидит учитель)" BOT_NAME "Ученик"
  ask "Логин для входа на сайт" APP_USER "student"
  ask "Пароль для входа на сайт (Enter — придумаю сам)" APP_PASSWORD ""
  [ -n "$APP_PASSWORD" ] || APP_PASSWORD=$(tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 16)
  [ -n "${GITHUB_TOKEN:-}" ] || ask "Токен GitHub (github_pat_..., с правом Contents: Read and write)" GITHUB_TOKEN ""
  cp .env.example .env
  python3 - "$BOT_NAME" "$APP_USER" "$APP_PASSWORD" "$GITHUB_TOKEN" <<'PY'
import sys, re
bot, user, pw, token = sys.argv[1:]
s = open(".env", encoding="utf-8").read()
for k, v in (("BOT_NAME", bot), ("APP_USER", user), ("APP_PASSWORD", pw), ("GITHUB_TOKEN", token)):
    s = re.sub(rf"^{k}=.*$", f"{k}={v}", s, flags=re.M)
open(".env", "w", encoding="utf-8").write(s)
PY
  chmod 600 .env
else
  say "3/5 Настройки уже есть в $DIR/.env — оставляю как есть"
  APP_USER=$(grep -E '^APP_USER=' .env | cut -d= -f2-)
  APP_PASSWORD=$(grep -E '^APP_PASSWORD=' .env | cut -d= -f2-)
fi

say "4/5 Проверяю, что с сервера открываются нужные сайты"
check() {  # check "Название" URL [код, который означает «доступ запрещён»]
  local code
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$2" || true)
  if [ "$code" = "000" ]; then echo "  ✗ $1 — НЕ открывается ($2)"
  elif [ -n "${3:-}" ] && [ "$code" = "$3" ]; then echo "  ✗ $1 — доступ запрещён из страны сервера (HTTP $code)"
  else echo "  ✓ $1"; fi
}
check "MTS Link" https://my.mts-link.ru/
check "GitHub" https://github.com/
check "Электронный журнал" https://storage01.eljur.ru/

say "5/5 Собираю и запускаю (первый раз ~10 минут)"
docker compose up -d --build

IP=$(curl -s -m 10 https://ifconfig.me || hostname -I | awk '{print $1}')
cat <<EOF

✅ Готово!
   Сайт:   http://$IP:8000
   Логин:  $APP_USER
   Пароль: $APP_PASSWORD

   Логи:        cd $DIR && docker compose logs -f
   Обновление:  curl -fsSL https://raw.githubusercontent.com/MissEmily10/StudyingTool/master/install.sh | sudo bash
EOF
