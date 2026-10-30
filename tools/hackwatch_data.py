import json, requests, time
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
d = None
for path in ("/api/data", "/snapshot.json"):
    try:
        r = S.get("https://binance-hack-watch.vercel.app" + path, timeout=30)
        if r.ok:
            d = r.json(); d["_from"] = path; break
    except Exception as e:
        print(path, e)
json.dump(d, open("data/hackwatch_raw.json", "w"), ensure_ascii=False)
print("keys", list(d.keys()) if isinstance(d, dict) else type(d))
