"""빗썸 v1 API로 원화마켓 전 종목 주봉 전체 기간 수집 → data/weekly.json"""
import json, time, requests
from concurrent.futures import ThreadPoolExecutor
S = requests.Session()
def get(url, params):
    for i in range(4):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            time.sleep(1 + i)
        except Exception:
            time.sleep(1 + i)
    return None
markets = [m for m in get("https://api.bithumb.com/v1/market/all", {"isDetails": "false"}) if m["market"].startswith("KRW-")]
names = {m["market"][4:]: m.get("korean_name", "") for m in markets}
def weeks(mk):
    rows, to = [], None
    for _ in range(10):
        p = {"market": mk, "count": 200}
        if to: p["to"] = to
        j = get("https://api.bithumb.com/v1/candles/weeks", p)
        if not j: break
        rows += j
        to = j[-1]["candle_date_time_kst"]
        if len(j) < 200: break
        time.sleep(0.1)
    rows.sort(key=lambda r: r["candle_date_time_kst"])
    return mk[4:], [[r["candle_date_time_kst"][:10], r["opening_price"], r["high_price"], r["low_price"],
                     r["trade_price"], round(r["candle_acc_trade_price"])] for r in rows]
out = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for tk, rows in ex.map(weeks, [m["market"] for m in markets]):
        if rows:
            out[tk] = rows
json.dump({"names": names, "weeks": out}, open("data/weekly.json", "w"), ensure_ascii=False, separators=(",", ":"))
print("coins", len(out), "weeks total", sum(len(v) for v in out.values()))
