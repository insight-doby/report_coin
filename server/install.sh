#!/bin/bash
# 계좌 비서 설치 스크립트 (Ubuntu 24.04 / Lightsail)
#   설치:     curl -fsSL https://raw.githubusercontent.com/insight-doby/report_coin/main/server/install.sh | sudo bash
#   업데이트: sudo coinbot update      키 다시 넣기: sudo coinbot keys
set -euo pipefail
REPO="insight-doby/report_coin"
RAW="https://raw.githubusercontent.com/$REPO/main/server"
ENVF=/etc/coinbot.env
MODE="${1:-install}"

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
say() { printf '\n\033[1;32m▶ %s\033[0m\n' "$*"; }

ask_secret() {   # 화면에 보이지 않게 입력받기
  local prompt="$1" v=""
  while [ -z "$v" ]; do
    printf '%s: ' "$prompt" > /dev/tty
    IFS= read -rs v < /dev/tty; echo > /dev/tty
    v=$(printf '%s' "$v" | sed 's/\x1b\[[0-9;]*[~a-zA-Z]//g' | tr -cd '[:graph:]')
    [ -z "$v" ] && echo "  Empty. Paste again." > /dev/tty
  done
  echo "  OK (${#v} chars)" > /dev/tty
  printf '%s' "$v"
}
ask_plain() {
  local prompt="$1" def="$2" v=""
  printf '%s [%s]: ' "$prompt" "$def" > /dev/tty
  IFS= read -r v < /dev/tty || true
  v=$(printf '%s' "$v" | sed 's/\x1b\[[0-9;]*[~a-zA-Z]//g' | tr -cd '[:graph:]')
  printf '%s' "${v:-$def}"
}

write_keys() {
  say "Enter keys (paste: Ctrl+Shift+V, input is hidden)"
  local ak sk tt tc gh
  ak=$(ask_secret "[1/5] Bithumb Access Key")
  sk=$(ask_secret "[2/5] Bithumb Secret Key")
  tt=$(ask_secret "[3/5] Telegram bot token")
  tc=$(ask_plain "[4/5] Telegram chat ID (just Enter)" "7266724379")
  printf '[5/5] GitHub token (optional, just Enter): ' > /dev/tty
  IFS= read -rs gh < /dev/tty || true; echo > /dev/tty
  gh=$(printf '%s' "$gh" | sed 's/\x1b\[[0-9;]*[~a-zA-Z]//g' | tr -cd '[:graph:]')
  umask 077
  cat > "$ENVF" <<EOF
BITHUMB_ACCESS_KEY=$ak
BITHUMB_SECRET_KEY=$sk
TELEGRAM_BOT_TOKEN=$tt
TELEGRAM_CHAT_ID=$tc
GITHUB_TOKEN=$gh
GITHUB_REPO=$REPO
COINBOT_DB=/var/lib/coinbot/coinbot.db
EOF
  chmod 600 "$ENVF"; chown root:root "$ENVF"
}

run_check() {
  say "Checking connection (no amounts shown)"
  ( set -a; . "$ENVF"; set +a; python3 /opt/coinbot/coinbot.py check )
}

if [ "$MODE" = keys ]; then
  write_keys; run_check && systemctl restart coinbot; exit 0
fi

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-requests ca-certificates curl >/dev/null
timedatectl set-timezone Asia/Seoul 2>/dev/null || true

if ! swapon --show | grep -q .; then
  say "Adding 1GB swap"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id coinbot >/dev/null 2>&1 || useradd --system --home /var/lib/coinbot --shell /usr/sbin/nologin coinbot
mkdir -p /opt/coinbot /var/lib/coinbot
chown coinbot:coinbot /var/lib/coinbot && chmod 700 /var/lib/coinbot

say "Downloading coinbot"
# 최신 커밋을 직접 지정해서 받기 (raw 주소 캐시 때문에 옛 버전을 받는 일 방지)
SHA=$(curl -fsSL "https://api.github.com/repos/$REPO/commits/main" | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
SRC="https://raw.githubusercontent.com/$REPO/${SHA:-main}/server"
curl -fsSL "$SRC/coinbot.py" -o /opt/coinbot/coinbot.py
curl -fsSL "$SRC/install.sh" -o /opt/coinbot/install.sh
echo "  version: ${SHA:0:7}"
chmod 755 /opt/coinbot/coinbot.py /opt/coinbot/install.sh

cat > /etc/systemd/system/coinbot.service <<'EOF'
[Unit]
Description=Bithumb account assistant
After=network-online.target
Wants=network-online.target

[Service]
User=coinbot
EnvironmentFile=/etc/coinbot.env
ExecStart=/usr/bin/python3 -u /opt/coinbot/coinbot.py run
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/coinbot
MemoryMax=300M

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/coinbot <<'EOF'
#!/bin/bash
case "${1:-status}" in
  status)  systemctl status coinbot --no-pager | head -12 ;;
  log)     journalctl -u coinbot -n "${2:-40}" --no-pager ;;
  report)  sudo bash -c 'set -a; . /etc/coinbot.env; set +a; sudo -E -u coinbot python3 /opt/coinbot/coinbot.py report' ;;
  check)   sudo bash -c 'set -a; . /etc/coinbot.env; set +a; python3 /opt/coinbot/coinbot.py check' ;;
  restart) sudo systemctl restart coinbot && echo "restarted" ;;
  update)  S=$(curl -fsSL https://api.github.com/repos/insight-doby/report_coin/commits/main | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
           curl -fsSL "https://raw.githubusercontent.com/insight-doby/report_coin/${S:-main}/server/install.sh" | sudo bash -s update ;;
  keys)    sudo bash /opt/coinbot/install.sh keys ;;
  *) echo "usage: coinbot [status|log|report|check|restart|update|keys]" ;;
esac
EOF
chmod 755 /usr/local/bin/coinbot

if [ ! -s "$ENVF" ]; then
  write_keys
fi

if ! run_check; then
  echo
  echo "FAILED: check Bithumb allowed IP / permissions, then run: sudo coinbot keys"
  exit 1
fi

systemctl daemon-reload
systemctl enable coinbot >/dev/null 2>&1
systemctl restart coinbot
say "DONE! Check Telegram"
echo "  coinbot status | coinbot log | sudo coinbot update"
