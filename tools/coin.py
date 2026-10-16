"""tools/coin_list.txt 의 종목 일봉·4시간·1시간·30분 각 200개 → data/coin_now.json (즉석 확인용)"""
import json, time, requests
S = requests.Session()
def get(url, p):
    for i in range(4):
        try:
            r = S.get(url, params=p, timeout=15)
            if r.status_code == 200: return r.json()
        except Exception: pass
        time.sleep(1 + i)
    return None
coins = [c.strip() for c in open("tools/coin_list.txt") if c.strip()]
out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "coins": {}}
for tk in coins:
    d = {}
    for k, path in (("d", "days"), ("h4", "minutes/240"), ("h1", "minutes/60"), ("m30", "minutes/30")):
        j = get(f"https://api.bithumb.com/v1/candles/{path}", {"market": f"KRW-{tk}", "count": 200}) or []
        d[k] = [[r["candle_date_time_kst"], r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], round(r["candle_acc_trade_price"])] for r in sorted(j, key=lambda r: r["candle_date_time_kst"])]
    out["coins"][tk] = d
json.dump(out, open("data/coin_now.json", "w"), ensure_ascii=False, separators=(",", ":"))
print({k: {a: len(b) for a, b in v.items()} for k, v in out["coins"].items()})
