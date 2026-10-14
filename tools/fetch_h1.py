"""빗썸 v1 1시간봉 약 100일 (거래대금 3억↑ 종목 + 섹터 목록 종목) → data/h1.json.gz"""
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
uni = {k for k, v in tick.items() if k != "date" and k not in STABLE and float(v.get("acc_trade_value_24H") or 0) >= 3e8}
sec = json.load(open("data/sectors.json"))["members"]
uni |= {x["tk"] for v in sec.values() for x in v if x["tk"] in tick and x["tk"] not in STABLE}
def one(tk):
    rows, to = [], None
    for _ in range(12):
        p = {"market": f"KRW-{tk}", "count": 200}
        if to: p["to"] = to
        j = get("https://api.bithumb.com/v1/candles/minutes/60", p)
        if not j: break
        rows += j; to = j[-1]["candle_date_time_kst"]
        if len(j) < 200: break
        time.sleep(0.05)
    rows.sort(key=lambda r: r["candle_date_time_kst"])
    return tk, [[r["candle_date_time_kst"][:13], r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], round(r["candle_acc_trade_price"])] for r in rows]
out = {}
with ThreadPoolExecutor(max_workers=5) as ex:
    for tk, rows in ex.map(one, sorted(uni)):
        if rows: out[tk] = rows
with gzip.open("data/h1.json.gz", "wt", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"))
print("coins", len(out))
