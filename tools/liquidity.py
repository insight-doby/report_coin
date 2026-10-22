"""거래대금 구간별 변동성 (즉석 분석) → data/liquidity.json
원화마켓 전 종목 24시간 거래대금 + 최근 60일 일봉으로
  - 하루 평균 변동폭(고가-저가)/종가
  - 3일 안에 +15% 이상 닿은 날 비율 (그날 종가에 샀다고 가정)
  - 최근 60일 일평균 거래대금
"""
import json, time, statistics, requests
S = requests.Session()


def get(url, p):
    for i in range(4):
        try:
            r = S.get(url, params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + i)
    return None


mk = [m["market"] for m in get("https://api.bithumb.com/v1/market/all", {"isDetails": "false"}) or [] if m["market"].startswith("KRW-")]
tick = {}
for i in range(0, len(mk), 50):
    for t in get("https://api.bithumb.com/v1/ticker", {"markets": ",".join(mk[i:i + 50])}) or []:
        tick[t["market"][4:]] = float(t.get("acc_trade_price_24h") or 0)
rows = []
for tk, v24 in sorted(tick.items(), key=lambda x: -x[1]):
    if v24 < 1e8 or tk in ("USDT", "USDC", "USD1", "USDE"):
        continue
    j = get("https://api.bithumb.com/v1/candles/days", {"market": f"KRW-{tk}", "count": 63}) or []
    j = sorted(j, key=lambda r: r["candle_date_time_kst"])[:-1]          # 진행 중인 오늘 봉 제외
    if len(j) < 40:
        continue
    c = [r["trade_price"] for r in j]
    h = [r["high_price"] for r in j]
    l = [r["low_price"] for r in j]
    val = [r["candle_acc_trade_price"] for r in j]
    rng = statistics.mean((h[i] - l[i]) / c[i] * 100 for i in range(len(c)))
    hits = [max(h[i + 1:i + 4]) / c[i] - 1 >= 0.15 for i in range(len(c) - 3)]
    rows.append({"tk": tk, "v24": round(v24), "v60": round(statistics.mean(val)), "range": round(rng, 2),
                 "hit15_3d": round(sum(hits) / len(hits) * 100, 1), "ret60": round((c[-1] / c[0] - 1) * 100, 1)})
json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}, open("data/liquidity.json", "w"), ensure_ascii=False)
print(len(rows))
