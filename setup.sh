#!/bin/bash
# Установка бота на Ubuntu: bash /opt/budget-bot/setup.sh
set -e
APP=/opt/budget-bot
DATA=/var/lib/budget-bot

echo "== Устанавливаю системные пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv python3-pip git unattended-upgrades fonts-dejavu-core ufw

echo "== Защита сервера: файрвол и автообновления =="
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== Ставлю зависимости Python =="
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
mkdir -p "$DATA"

if [ ! -f "$APP/.env" ]; then
  echo
  read -rsp "Вставь токен бота от BotFather (символы не видны) и нажми Enter: " TOKEN; echo
  cat > "$APP/.env" <<EOF
BOT_TOKEN=$TOKEN
ALLOWED_IDS=
DB_PATH=$DATA/budget.db
NOTIFY_PARTNER=0
EOF
  chmod 600 "$APP/.env"
fi

cat > /etc/systemd/system/budget-bot.service <<EOF
[Unit]
Description=Telegram budget bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now budget-bot
sleep 3
systemctl --no-pager --lines=5 status budget-bot || true
echo
echo "Готово. Напиши боту в Telegram — он ответит твоим ID."
echo "Потом выполни: bash $APP/allow.sh ТВОЙ_ID"
