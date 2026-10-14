"""빗썸 v1 일봉 (거래대금 3억 이상, 최대 2년) + 월러스 1시간봉 → data/d1.json.gz"""
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
uni = [k for k, v in tick.items() if k != "date" and k not in STABLE and float(v.get("acc_trade_value_24H") or 0) >= 3e8]
if "BTC" not in uni: uni.append("BTC")
def pages(path, mk, maxp):
    rows, to = [], None
    for _ in range(maxp):
        p = {"market": mk, "count": 200}
        if to: p["to"] = to
        j = get(f"https://api.bithumb.com/v1/candles/{path}", p)
        if not j: break
        rows += j; to = j[-1]["candle_date_time_kst"]
        if len(j) < 200: break
        time.sleep(0.08)
    rows.sort(key=lambda r: r["candle_date_time_kst"])
    return [[r["candle_date_time_kst"][:13], r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], round(r["candle_acc_trade_price"])] for r in rows]
out = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for tk, rows in ex.map(lambda tk: (tk, pages("days", f"KRW-{tk}", 4)), uni):
        if rows: out[tk] = rows
wal_h = pages("minutes/60", "KRW-WAL", 2)
with gzip.open("data/d1.json.gz", "wt", encoding="utf-8") as f:
    json.dump({"days": out, "wal_1h": wal_h, "tick": {k: tick[k] for k in uni}}, f, separators=(",", ":"))
print("coins", len(out), "WAL 1h", len(wal_h))
