#!/usr/bin/env bash
# Удаление VoiceChat — NightLord РВЕ. VPN не затрагивается.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: sudo bash uninstall.sh"; exit 1; }
DIR=/opt/voicechat
PORT="$(grep -oP '^VC_PORT=\K.*' "$DIR/voicechat.env" 2>/dev/null || echo 8443)"

systemctl disable --now voicechat 2>/dev/null || true
rm -f /etc/systemd/system/voicechat.service
systemctl daemon-reload

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw delete allow "$PORT/tcp" >/dev/null 2>&1 || true
elif command -v firewall-cmd >/dev/null && firewall-cmd --state &>/dev/null; then
  firewall-cmd --permanent --remove-port="$PORT/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null || true
fi

read -r -p "Удалить историю сообщений и голосовые ($DIR)? [y/N] " a
if [[ "$a" =~ ^[yYдД]$ ]]; then rm -rf "$DIR"; userdel voicechat 2>/dev/null || true; echo "Удалено полностью."
else echo "Служба удалена, данные оставлены в $DIR"; fi
