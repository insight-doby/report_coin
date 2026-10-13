"""빗썸 v1 주봉/일봉이 얼마나 과거까지 나오는지 확인"""
import json, time, requests
out = {}
def pages(path, market, maxpages=20):
    rows, to = [], None
    for _ in range(maxpages):
        p = {"market": market, "count": 200}
        if to: p["to"] = to
        r = requests.get(f"https://api.bithumb.com/v1/candles/{path}", params=p, timeout=15)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code} {r.text[:200]}", "n": len(rows)}
        j = r.json()
        if not j: break
        rows += j
        to = j[-1]["candle_date_time_kst"]
        if len(j) < 200: break
        time.sleep(0.15)
    return {"n": len(rows), "oldest": rows[-1]["candle_date_time_kst"] if rows else None,
            "newest": rows[0]["candle_date_time_kst"] if rows else None, "sample": rows[:2]}
for m in ["KRW-BTC", "KRW-ETH", "KRW-AVAX", "KRW-XRP", "KRW-AZTEC"]:
    out[m] = {"weeks": pages("weeks", m), "days": pages("days", m, 5)}
# 공개 API에 키가 필요한지 / 구 API 비교
r = requests.get("https://api.bithumb.com/public/candlestick/BTC_KRW/24h", timeout=15).json()
out["old_api_days"] = len(r.get("data", []))
json.dump(out, open("data/probe.json", "w"), ensure_ascii=False, indent=1)
for k, v in out.items(): print(k, v if not isinstance(v, dict) else {a: {x: y for x, y in b.items() if x != "sample"} for a, b in v.items()})
