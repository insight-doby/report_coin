import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
H = bot.HOUR_MS; t0 = 1_800_000_000_000
n = 30
t = np.array([t0 + i * H for i in range(n)], float)
c = np.full(n, 675.0); o = c.copy(); v = np.full(n, 100.0)
o[-1], c[-1], v[-1] = 675, 729, 680       # 월드코인 10/16 17시 봉과 비슷
cd = {"t": t, "o": o, "h": c + 3, "l": np.minimum(o, c) - 2, "c": c, "v": v}
flat = {"t": t, "o": np.full(n, 10.0), "h": np.full(n, 10.1), "l": np.full(n, 9.9), "c": np.full(n, 10.0), "v": np.full(n, 100.0)}
bot.fetch_v1 = lambda tk, tf, k=30: cd if tk == "WLD" else flat
sent = []
bot.slack_post = lambda text, blocks, thread_ts=None, tg=True: sent.append(text) or "1"
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    s = bot.Store()
    latest = {"uptrend": ["WLD", "ADA"], "watch": []}
    at = t0 + n * H + 20 * 60000
    rows = bot.scan_hourly_moves(s, latest, at)
    assert [r["ticker"] for r in rows] == ["WLD"] and rows[0]["kind"] == "move", rows
    bot.slack_moves(rows); assert len(sent) == 1
    assert bot.scan_hourly_moves(s, latest, at + H) == []      # 24시간 안 중복 없음
print("MOVES OK")
