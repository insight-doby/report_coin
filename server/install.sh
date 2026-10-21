#!/bin/bash
# 계좌 비서 설치 스크립트 (Ubuntu 24.04 / Lightsail)
#   설치:     curl -fsSL https://raw.githubusercontent.com/insight-doby/report_coin/main/server/install.sh | sudo bash
#   업데이트: sudo coinbot update      키 다시 넣기: sudo coinbot keys
set -euo pipefail
REPO="insight-doby/report_coin"
RAW="https://raw.githubusercontent.com/$REPO/main/server"
ENVF=/etc/coinbot.env
MODE="${1:-install}"

[ "$(id -u)" = 0 ] || { echo "sudo 를 붙여서 실행해 주세요"; exit 1; }
say() { printf '\n\033[1;32m▶ %s\033[0m\n' "$*"; }

ask_secret() {   # 화면에 보이지 않게 입력받기
  local prompt="$1" v=""
  while [ -z "$v" ]; do
    printf '%s: ' "$prompt" > /dev/tty
    IFS= read -rs v < /dev/tty; echo > /dev/tty
    v=$(printf '%s' "$v" | sed 's/\x1b\[[0-9;]*[~a-zA-Z]//g' | tr -cd '[:graph:]')
    [ -z "$v" ] && echo "  비어 있어요. 다시 붙여넣어 주세요." > /dev/tty
  done
  echo "  ✓ 입력됨 (${#v}자)" > /dev/tty
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
  say "키 입력 (붙여넣기: Ctrl+Shift+V 또는 마우스 오른쪽 클릭 · 화면엔 안 보여요)"
  local ak sk tt tc gh
  ak=$(ask_secret "빗썸 Access Key")
  sk=$(ask_secret "빗썸 Secret Key")
  tt=$(ask_secret "텔레그램 봇 토큰 (BotFather가 준 것)")
  tc=$(ask_plain "텔레그램 채팅 ID" "7266724379")
  printf '대시보드 자동 반영용 GitHub 토큰 (없으면 그냥 엔터): ' > /dev/tty
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
  say "연결 확인 (금액은 표시하지 않아요)"
  ( set -a; . "$ENVF"; set +a; python3 /opt/coinbot/coinbot.py check )
}

if [ "$MODE" = keys ]; then
  write_keys; run_check && systemctl restart coinbot; exit 0
fi

say "기본 프로그램 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-requests ca-certificates curl >/dev/null
timedatectl set-timezone Asia/Seoul 2>/dev/null || true

if ! swapon --show | grep -q .; then
  say "메모리 보조(스왑 1GB) 설정"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id coinbot >/dev/null 2>&1 || useradd --system --home /var/lib/coinbot --shell /usr/sbin/nologin coinbot
mkdir -p /opt/coinbot /var/lib/coinbot
chown coinbot:coinbot /var/lib/coinbot && chmod 700 /var/lib/coinbot

say "계좌 비서 내려받기"
curl -fsSL "$RAW/coinbot.py?$(date +%s)" -o /opt/coinbot/coinbot.py
curl -fsSL "$RAW/install.sh?$(date +%s)" -o /opt/coinbot/install.sh
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
  restart) sudo systemctl restart coinbot && echo "재시작했어요" ;;
  update)  curl -fsSL "https://raw.githubusercontent.com/insight-doby/report_coin/main/server/install.sh?$(date +%s)" | sudo bash -s update ;;
  keys)    sudo bash /opt/coinbot/install.sh keys ;;
  *) echo "사용법: coinbot [status|log|report|check|restart|update|keys]" ;;
esac
EOF
chmod 755 /usr/local/bin/coinbot

if [ ! -s "$ENVF" ]; then
  write_keys
fi

if ! run_check; then
  echo
  echo "❌ 빗썸 연결에 실패했어요. 빗썸 API 관리에서 허용 IP와 권한(자산조회·주문조회)을 확인한 뒤"
  echo "   sudo coinbot keys  로 키를 다시 넣어 주세요."
  exit 1
fi

systemctl daemon-reload
systemctl enable coinbot >/dev/null 2>&1
systemctl restart coinbot
say "설치 완료! 텔레그램을 확인해 보세요 🎉"
echo "  상태 보기: coinbot status   로그: coinbot log   업데이트: sudo coinbot update"
