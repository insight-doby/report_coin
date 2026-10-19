import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
H = bot.HOUR_MS * 4
t0 = 1_800_000_000_000
t = np.array([t0 + i * H for i in range(30)], float)
c = np.full(30, 310.0); v = np.full(30, 100.0)
c[25] = 315; v[25] = 80        # 가격만 넘음 (약함)
c[27] = 316; v[27] = 300       # 거래량 3배 돌파
cd = {"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": v}
bot.fetch_v1 = lambda tk, tf, n=60: cd
sent = []
bot.slack_post = lambda text, blocks, thread_ts=None, tg=True: sent.append(blocks) or "1"
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    al = {"id": "x", "ticker": "ADA", "tf": "4h", "cond": "close_above", "price": 314, "vol_x": 1.5, "status": "active",
          "created_ts": t0 + 20 * H, "action": "매수 고려"}
    json.dump({"alerts": [al]}, open(os.path.join(td, "price_alerts.json"), "w"))
    s = bot.Store()
    bot.run_price_alerts(s, t0 + 27 * H)          # 26번 봉까지 마감 → 약한 돌파만
    a = json.load(open(os.path.join(td, "price_alerts.json")))["alerts"][0]
    assert a["status"] == "active" and a.get("weak_sent"), a
    bot.run_price_alerts(s, t0 + 28 * H)          # 27번 봉 마감 → 진짜 돌파
    a = json.load(open(os.path.join(td, "price_alerts.json")))["alerts"][0]
    assert a["status"] == "fired" and a["fired_bar"]["close"] == 316, a
    n = len(sent)
    bot.run_price_alerts(s, t0 + 29 * H)          # 다시 울리지 않음
    assert len(sent) == n == 2, sent
print("ALERTS OK")
