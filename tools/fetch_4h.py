"""빗썸 v1 240분봉 수집 (24h 거래대금 10억 이상 + 메이저) → data/h4.json.gz
각 행: [KST 시작시각 'YYYY-MM-DDTHH', 시가, 고가, 저가, 종가, 거래대금]"""
import json, gzip, time, requests
from concurrent.futures import ThreadPoolExecutor
S = requests.Session()
def get(url, params=None):
    for i in range(4):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200: return r.json()
            time.sleep(1 + i)
        except Exception: time.sleep(1 + i)
    return None
tick = get("https://api.bithumb.com/public/ticker/ALL_KRW")["data"]
STABLE = {"USDT","USDC","DAI","USDS","USDE","USD1","PYUSD","FDUSD","TUSD"}
uni = [k for k, v in tick.items() if k != "date" and k not in STABLE and float(v.get("acc_trade_value_24H") or 0) >= 1e9]
for m in ["BTC","ETH","XRP","SOL","BNB","ADA","DOGE","TRX","LINK","AVAX","DOT","BCH","XLM","ETC","SUI"]:
    if m in tick and m not in uni: uni.append(m)
MAXP = 22                                     # 22 × 200봉 ≈ 2년
def one(tk):
    rows, to = [], None
    for _ in range(MAXP):
        p = {"market": f"KRW-{tk}", "count": 200}
        if to: p["to"] = to
        j = get("https://api.bithumb.com/v1/candles/minutes/240", p)
        if not j: break
        rows += j; to = j[-1]["candle_date_time_kst"]
        if len(j) < 200: break
        time.sleep(0.08)
    rows.sort(key=lambda r: r["candle_date_time_kst"])
    return tk, [[r["candle_date_time_kst"][:13], r["opening_price"], r["high_price"], r["low_price"], r["trade_price"],
                 round(r["candle_acc_trade_price"])] for r in rows]
out = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for tk, rows in ex.map(one, uni):
        if rows: out[tk] = rows
with gzip.open("data/h4.json.gz", "wt", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"))
print("coins", len(out), "bars", sum(len(v) for v in out.values()), "oldest BTC", out.get("BTC", [[None]])[0][0])
