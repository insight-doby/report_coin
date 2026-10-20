import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
rng = np.random.default_rng(1)
def series(n, start, drift):
    c = start * np.cumprod(1 + rng.normal(drift, 0.04, n)); o = np.r_[c[0], c[:-1]]
    return {"t": np.arange(n, dtype=float), "o": o, "h": np.maximum(o, c) * 1.03, "l": np.minimum(o, c) * 0.97, "c": c, "v": np.full(n, 1e9)}
W = {tk: series(150, 100, 0.01 if tk != "BAD" else -0.01) for tk in ("BTC", "ETH", "AAA", "BAD")}
D = {tk: series(200, W[tk]["c"][-1] / 1.2, 0.001) for tk in W}
for tk in D: D[tk]["c"][-1] = W[tk]["c"][-1]
bot.fetch_v1 = lambda tk, tf, n=60: (W if tf == "1w" else D).get(tk)
bot.fetch_tickers_all = lambda: {tk: {"price": float(W[tk]["c"][-1]), "chg": 0, "value": 5e9} for tk in W}
sent = []
bot.slack_post = lambda text, blocks, thread_ts=None, **kw: sent.append(text) or "1"
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    json.dump({"watch": ["BTC", "ETH"], "manual": {"ETH": {"zones": [[1, 2, "x", 50]], "inval": 0.5}}}, open(os.path.join(td, "accumulate_cfg.json"), "w"))
    out = bot.run_accumulate()
    it = {i["ticker"]: i for i in out["items"]}
    z = it["BTC"]["zones"]
    assert it["BTC"]["source"] == "auto" and len(z) == 3 and it["ETH"]["source"] == "manual"
    p = it["BTC"]["price"]
    assert all(zz["hi"] < p for zz in z) and z[0]["hi"] > z[1]["hi"] > z[2]["hi"] > it["BTC"]["inval"], z
    al = json.load(open(os.path.join(td, "price_alerts.json")))["alerts"]
    assert sorted(a["id"] for a in al) == ["ACC-BTC-1", "ACC-BTC-2", "ACC-BTC-3", "ACC-BTC-inval"], al
    bot.run_accumulate()                                    # 두 번 돌려도 알림 중복 없음
    al = json.load(open(os.path.join(td, "price_alerts.json")))["alerts"]
    assert len(al) == 4
    assert "BAD" not in [c["ticker"] for c in out["candidates"]]
    print("ACC OK", [(round(zz["lo"], 2), round(zz["hi"], 2), zz["why"][:2]) for zz in z], "후보", [c["ticker"] for c in out["candidates"]])
