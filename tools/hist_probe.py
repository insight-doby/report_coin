"""과거 자료가 어디까지 있는지 확인 (공개 자료만). → data/mm_audit/hist_probe.json
  1) 빗썸 REST 체결: daysAgo / to 로 며칠 전까지 받을 수 있나
  2) 빗썸 1분봉: 몇 년 전까지 있나
  3) Tardis 무료 표본(매달 1일): 빗썸 체결·호가 파일이 실제로 받아지는가, 크기와 형식
"""
import gzip, io, json, time
import requests
S = requests.Session(); S.headers["User-Agent"] = "report-coin-research"
out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ticks": {}, "candles": {}, "tardis": {}}
B = "https://api.bithumb.com/v1"
for da in (0, 1, 3, 7, 8, 30):
    try:
        p = {"market": "KRW-XRP", "count": 5}
        if da: p["daysAgo"] = da
        r = S.get(B + "/trades/ticks", params=p, timeout=15)
        j = r.json()
        out["ticks"][f"daysAgo={da}"] = {"http": r.status_code, "rows": [[x.get("trade_date_utc"), x.get("trade_time_utc"), x.get("timestamp")] for x in j[:2]] if isinstance(j, list) else str(j)[:200]}
    except Exception as e:
        out["ticks"][f"daysAgo={da}"] = {"err": str(e)[:200]}
    time.sleep(0.2)
for to in ("2026-11-04 00:00:00", "2026-10-01 00:00:00", "2026-01-01 00:00:00"):
    try:
        r = S.get(B + "/trades/ticks", params={"market": "KRW-XRP", "count": 3, "to": to.split()[1].replace(":", "")}, timeout=15)
        out["ticks"][f"to={to}(시각만)"] = {"http": r.status_code, "body": str(r.json())[:200]}
    except Exception as e:
        out["ticks"][f"to={to}"] = {"err": str(e)[:200]}
for to in ("2026-10-30T00:00:00", "2025-11-21T00:00:00", "2024-08-05T12:00:00", "2022-11-10T00:00:00", "2022-06-15T00:00:00", "2021-05-19T12:00:00"):
    for mk in ("KRW-XRP", "KRW-BTC"):
        try:
            r = S.get(B + "/candles/minutes/1", params={"market": mk, "count": 3, "to": to}, timeout=15)
            j = r.json()
            out["candles"][f"{mk} to={to}"] = {"http": r.status_code, "rows": [[x.get("candle_date_time_kst"), x.get("trade_price"), x.get("candle_acc_trade_volume")] for x in j] if isinstance(j, list) else str(j)[:200]}
        except Exception as e:
            out["candles"][f"{mk} to={to}"] = {"err": str(e)[:200]}
        time.sleep(0.2)
T = "https://datasets.tardis.dev/v1/bithumb"
for day in ("2026/10/01", "2026/11/01"):
    for kind in ("trades", "book_snapshot_5", "incremental_book_L2", "quotes"):
        for sym in ("KRW-XRP", "KRW-BTC", "KRW-USDT"):
            url = f"{T}/{kind}/{day}/{sym}.csv.gz"
            try:
                r = S.get(url, stream=True, timeout=30)
                info = {"http": r.status_code, "len": r.headers.get("content-length")}
                if r.ok and kind in ("trades", "book_snapshot_5") and sym == "KRW-XRP" and day == "2026/10/01":
                    raw = r.raw.read(200_000)
                    try:
                        txt = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(6000).decode("utf-8", "replace")
                    except Exception as e:
                        txt = "gunzip 실패 " + str(e)[:100]
                    info["head"] = txt.splitlines()[:6]
                r.close()
            except Exception as e:
                info = {"err": str(e)[:200]}
            out["tardis"][f"{kind}/{day}/{sym}"] = info
import os
os.makedirs("data/mm_audit", exist_ok=True)
json.dump(out, open("data/mm_audit/hist_probe.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1)[:6000])
