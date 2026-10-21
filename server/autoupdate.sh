#!/bin/bash
# 10분마다 실행: GitHub 최신 버전이 바뀌었고 문법 검사를 통과하면 교체 후 재시작
set -u
REPO=insight-doby/report_coin
SHA=$(curl -fsSL -m 20 "https://api.github.com/repos/$REPO/commits/main" \
      | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
[ -n "$SHA" ] || exit 0
[ "$(cat /opt/coinbot/VERSION 2>/dev/null)" = "$SHA" ] && exit 0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
BASE="https://raw.githubusercontent.com/$REPO/$SHA/server"
for f in coinbot.py install.sh autoupdate.sh; do
  curl -fsSL -m 30 "$BASE/$f" -o "$TMP/$f" || exit 0
done
python3 -m py_compile "$TMP/coinbot.py" || { echo "autoupdate: compile failed, skip ${SHA:0:7}"; exit 0; }
bash -n "$TMP/autoupdate.sh" || exit 0
if ! cmp -s "$TMP/coinbot.py" /opt/coinbot/coinbot.py; then
  cp /opt/coinbot/coinbot.py /opt/coinbot/coinbot.prev.py 2>/dev/null || true
  install -m 755 "$TMP/coinbot.py" /opt/coinbot/coinbot.py
  systemctl restart coinbot
  echo "autoupdate: coinbot -> ${SHA:0:7}"
fi
install -m 755 "$TMP/install.sh" /opt/coinbot/install.sh
install -m 755 "$TMP/autoupdate.sh" /usr/local/bin/coinbot-autoupdate
echo "$SHA" > /opt/coinbot/VERSION
