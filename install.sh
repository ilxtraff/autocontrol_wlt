#!/usr/bin/env bash
# Полная установка на чистый сервер: зависимости, код, служба, автозапуск.
# Запускать от root:  bash install.sh
set -euo pipefail

REPO="${REPO:-https://github.com/ilxtraff/autocontrol_wlt.git}"
BRANCH="${BRANCH:-claude/admiring-bohr-tcaj3e}"
DEST="${DEST:-/opt/autocontrol_wlt}"
PORT="${PORT:-8000}"
ADMIN="${ADMIN:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
USING_SYSTEMD=""

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --admin) ADMIN="$2"; shift 2 ;;
        --password) ADMIN_PASSWORD="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        *) echo "неизвестный параметр: $1"; exit 1 ;;
    esac
done

if [ "$(id -u)" != "0" ]; then
    echo "Запустите от root: sudo bash install.sh"
    exit 1
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip git curl
else
    echo "Не нашёл apt или dnf — поставьте python3, python3-venv и git вручную."
    exit 1
fi

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
    3.1[1-9]|3.[2-9]*) ;;
    *) echo "Нужен Python 3.11+, на сервере $PYVER"; exit 1 ;;
esac
echo "    Python $PYVER"

say "Код"
if [ -d "$DEST/.git" ]; then
    git -C "$DEST" fetch --quiet origin "$BRANCH"
    git -C "$DEST" checkout --quiet "$BRANCH"
    git -C "$DEST" reset --hard --quiet "origin/$BRANCH"
    echo "    обновлён $DEST"
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$DEST"
    echo "    склонирован в $DEST"
fi
cd "$DEST"

say "Окружение и зависимости"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

say "Настройки"
if [ -f .env ]; then
    echo "    .env уже есть — не трогаю"
else
    cp .env.example .env
    KEY="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    # Ключ шифрует мультитокены: генерируем один раз и больше не меняем.
    ./.venv/bin/python - "$KEY" "$PORT" <<'PY'
import pathlib, sys
key, port = sys.argv[1], sys.argv[2]
path = pathlib.Path(".env")
out = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("AC_SECRET_KEY="):
        line = f"AC_SECRET_KEY={key}"
    elif line.startswith("AC_DRY_RUN="):
        # Первый запуск вхолостую: движок считает, но адсеты не трогает.
        line = "AC_DRY_RUN=true"
    elif line.startswith("AC_BIND_PORT="):
        line = f"AC_BIND_PORT={port}"
    out.append(line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
    chmod 600 .env
    echo "    ключ сгенерирован, режим без действий включён"
fi

say "База и первый пользователь"
./.venv/bin/python -c "from app.db import init_db; init_db()"

HAS_USER="$(./.venv/bin/python -c "
from sqlalchemy import select
from app.db import session_scope
from app.models import User
with session_scope() as s:
    print('yes' if s.scalar(select(User).limit(1)) else 'no')
")"

CREATED_PASSWORD=""
if [ "$HAS_USER" = "yes" ]; then
    echo "    пользователи уже заведены"
else
    if [ -z "$ADMIN_PASSWORD" ]; then
        ADMIN_PASSWORD="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')"
        CREATED_PASSWORD="$ADMIN_PASSWORD"
    fi
    ./.venv/bin/python -m app.cli createuser "$ADMIN" --role admin --password "$ADMIN_PASSWORD"
fi

say "Служба"
# Наличие systemctl ещё не значит, что systemd — init: в LXC и части VPS
# бинарник есть, а шина не поднята, и daemon-reload валится с ошибкой.
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/autocontrol.service <<UNIT
[Unit]
Description=Автоконтроль адсетов
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DEST
ExecStart=$DEST/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --quiet --now autocontrol
    systemctl restart autocontrol
    USING_SYSTEMD="yes"
    echo "    autocontrol.service включён"
else
    rm -f /etc/systemd/system/autocontrol.service
    echo "    systemd не управляет этой машиной — поднимаю через nohup"
    pkill -f "uvicorn app.main:app" 2>/dev/null || true
    nohup "$DEST/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" \
        > "$DEST/autocontrol.log" 2>&1 &
    echo "    лог: $DEST/autocontrol.log"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
    echo "    порт $PORT открыт в ufw"
fi

say "Проверка"
OK=""
for _ in $(seq 1 30); do
    if curl -sf --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
        OK="yes"; break
    fi
    sleep 1
done

IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"

if [ -n "$OK" ]; then
    printf '\n\033[1;32mГотово.\033[0m Сервис работает.\n\n'
    echo "    Адрес:  http://$IP:$PORT/"
    echo "    Логин:  $ADMIN"
    if [ -n "$CREATED_PASSWORD" ]; then
        echo "    Пароль: $CREATED_PASSWORD"
        echo
        echo "    Пароль показан один раз — сохраните его."
    fi
    cat <<'NEXT'

Сейчас включён режим без действий: движок считает и пишет решения
в журнал, но адсеты не выключает. Это нарочно.

Дальше:
  cd /opt/autocontrol_wlt && source .venv/bin/activate
  python -m app.cli social-add "Камилла"     # мультитокен, ввод скрытый
  python -m app.cli fb-accounts              # проверить прокси и кабинеты

Потом в интерфейсе: Интеграции -> Keitaro и рекламные кабинеты,
затем python -m app.cli doctor

NEXT
    if [ -n "$USING_SYSTEMD" ]; then
        echo "Логи:       journalctl -u autocontrol -f"
        echo "Перезапуск: systemctl restart autocontrol"
    else
        echo "Логи:       tail -f $DEST/autocontrol.log"
    fi
else
    printf '\n\033[1;31mСервис не поднялся.\033[0m Посмотрите логи:\n'
    if [ -n "$USING_SYSTEMD" ]; then
        echo "    journalctl -u autocontrol -n 50 --no-pager"
    else
        echo "    tail -n 50 $DEST/autocontrol.log"
    fi
    exit 1
fi
