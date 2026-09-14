#!/usr/bin/env bash
# Первичная установка: окружение, зависимости, .env и первый пользователь.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Окружение Python"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Зависимости"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ -f .env ]; then
    echo "==> .env уже есть — не трогаю"
    if ! grep -qE '^AC_SECRET_KEY=.{20,}' .env; then
        echo
        echo "ВНИМАНИЕ: в .env не задан AC_SECRET_KEY."
        echo "Им шифруются мультитокены. Без него они не сохранятся."
        exit 1
    fi
else
    echo "==> Создаю .env"
    cp .env.example .env
    KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    # Ключ постоянный: он шифрует мультитокены, менять его потом нельзя.
    python - "$KEY" <<'PY'
import sys, pathlib
key = sys.argv[1]
path = pathlib.Path(".env")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("AC_SECRET_KEY="):
        line = f"AC_SECRET_KEY={key}"
    elif line.startswith("AC_DRY_RUN="):
        # Первый запуск всегда вхолостую: движок считает, но адсеты не трогает.
        line = "AC_DRY_RUN=true"
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
    chmod 600 .env
    echo "    ключ сгенерирован, режим без действий включён"
fi

echo "==> База данных"
python -c "from app.db import init_db; init_db()"

if python -c "
import sys
from sqlalchemy import select
from app.db import session_scope
from app.models import User
with session_scope() as s:
    sys.exit(0 if s.scalar(select(User).limit(1)) else 1)
" 2>/dev/null; then
    echo "    пользователи уже заведены"
else
    echo
    echo "==> Первый пользователь"
    read -r -p "    Логин [admin]: " LOGIN
    LOGIN="${LOGIN:-admin}"
    python -m app.cli createuser "$LOGIN" --role admin
fi

echo
echo "Готово. Запустить вручную:"
echo "    source .venv/bin/activate"
echo "    uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo
echo "Этот скрипт НЕ создаёт службу — сервис умрёт вместе с сессией."
echo "Чтобы он работал постоянно и перезапускался сам:"
echo "    sudo bash install.sh"
echo
echo "Дальше — шаги 7+ в НАСТРОЙКА.md: ключ Keitaro и мультитокен."
