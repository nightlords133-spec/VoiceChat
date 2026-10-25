#!/usr/bin/env bash
# Установка VoiceChat на VPS (Debian/Ubuntu) — NightLord РВЕ
# Ничего не меняет в 3x-ui / Xray: отдельный порт, отдельный пользователь, отдельная служба.
# Повторный запуск = обновление (пароль, история и сертификат сохраняются).
set -euo pipefail

DIR=/opt/voicechat
ENV="$DIR/voicechat.env"
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: sudo bash install.sh"; exit 1; }

if [ -f "$ENV" ]; then PORT="$(grep -oP '^VC_PORT=\K.*' "$ENV" || echo 8443)"; fi
PORT="${VC_PORT:-${PORT:-8443}}"

echo "==> Проверяю зависимости"
NEED=""
command -v python3 >/dev/null || NEED="$NEED python3"
command -v openssl >/dev/null || NEED="$NEED openssl"
if [ -n "$NEED" ]; then apt-get update -qq && apt-get install -y -qq $NEED; fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,7) else 1)' || { echo "Нужен Python 3.7+"; exit 1; }

echo "==> Проверяю порт $PORT"
if ss -ltnH "( sport = :$PORT )" | grep -q . && ! systemctl is-active --quiet voicechat; then
  echo "Порт $PORT уже занят (возможно, VPN или панель):"
  ss -ltnp "( sport = :$PORT )" || true
  echo "Выберите другой порт, например:  VC_PORT=9443 bash install.sh"
  exit 1
fi

echo "==> Копирую файлы в $DIR"
id voicechat &>/dev/null || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin voicechat
mkdir -p "$DIR/static" "$DIR/data"
install -m 644 "$SRC/server.py" "$DIR/server.py"
install -m 644 "$SRC/static/index.html" "$DIR/static/index.html"
chown -R voicechat:voicechat "$DIR/data"

if [ ! -f "$ENV" ]; then
  PASS="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 14)"
  printf 'VC_PORT=%s\nVC_PASSWORD=%s\n' "$PORT" "$PASS" > "$ENV"
else
  sed -i "s/^VC_PORT=.*/VC_PORT=$PORT/" "$ENV"
fi
chown root:root "$ENV"; chmod 600 "$ENV"

echo "==> Создаю службу voicechat"
cat > /etc/systemd/system/voicechat.service <<EOF
[Unit]
Description=VoiceChat - голосовой чат в браузере (NightLord РВЕ)
After=network.target

[Service]
User=voicechat
Group=voicechat
WorkingDirectory=$DIR
EnvironmentFile=$ENV
Environment=VC_DATA=$DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$(command -v python3) $DIR/server.py
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DIR/data

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable voicechat >/dev/null 2>&1
systemctl restart voicechat

echo "==> Открываю порт $PORT в фаерволе (если он включён)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null && echo "   ufw: порт $PORT/tcp открыт"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state &>/dev/null; then
  firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null && firewall-cmd --reload >/dev/null && echo "   firewalld: порт $PORT/tcp открыт"
else
  echo "   фаервол не активен — ничего не меняю"
fi

sleep 2
if ! systemctl is-active --quiet voicechat; then
  echo "Служба не запустилась. Лог:"; journalctl -u voicechat -n 30 --no-pager; exit 1
fi

IP="$(curl -s4 --max-time 4 https://api.ipify.org || hostname -I | awk '{print $1}')"
PASS="$(grep -oP '^VC_PASSWORD=\K.*' "$ENV")"
echo
echo "============================================"
echo "  VoiceChat запущен  (NightLord РВЕ)"
echo "  Адрес:   https://$IP:$PORT"
echo "  Пароль:  $PASS"
echo "============================================"
echo "Браузер предупредит о сертификате — нажмите «Дополнительно» → «Перейти на сайт»."
echo "Сменить пароль:  nano $ENV  и затем  systemctl restart voicechat"
