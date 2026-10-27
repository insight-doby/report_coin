"""빗썸 원화마켓 일봉을 최대한 길게 (최대 약 6년) → data/d1_long.json.gz  (지표 장기 검증용)
각 행: [KST 날짜, 시가, 고가, 저가, 종가, 거래대금]"""
import json, gzip, time, requests
from concurrent.futures import ThreadPoolExecutor
S = requests.Session()
def get(url, params=None):
    for i in range(5):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 200: return r.json()
        except Exception: pass
        time.sleep(1 + i)
    return None
mk = [m["market"][4:] for m in get("https://api.bithumb.com/v1/market/all", {"isDetails": "false"}) or [] if m["market"].startswith("KRW-")]
STABLE = {"USDT", "USDC", "USD1", "USDE", "USDS", "DAI", "PYUSD", "FDUSD", "TUSD"}
mk = [m for m in mk if m not in STABLE]
def one(tk):
    rows, to = [], None
    for _ in range(11):                      # 11 × 200 = 최대 2,200일
        p = {"market": f"KRW-{tk}", "count": 200}
        if to: p["to"] = to
        j = get("https://api.bithumb.com/v1/candles/days", p)
        if not j: break
        rows += j
        to = j[-1]["candle_date_time_kst"].replace("T", " ")
        if len(j) < 200: break
        time.sleep(0.07)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["candle_date_time_kst"]):
        d = r["candle_date_time_kst"][:10]
        if d in seen: continue
        seen.add(d)
        out.append([d, r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], round(r["candle_acc_trade_price"])])
    return tk, out
data = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for tk, rows in ex.map(one, mk):
        if len(rows) >= 150: data[tk] = rows
with gzip.open("data/d1_long.json.gz", "wt", encoding="utf-8") as f:
    json.dump(data, f, separators=(",", ":"))
print("coins", len(data), "BTC from", data.get("BTC", [[None]])[0][0], "rows", sum(len(v) for v in data.values()))
