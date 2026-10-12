import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import bot

DAY, HOUR = bot.DAY_MS, bot.HOUR_MS
rng = np.random.default_rng(7)
now = bot.now_ms()
TK = ["BTC", "ETH", "XRP", "SUI", "HBAR", "ADA", "DOGE", "LINK", "AVAX", "SOL", "USDT"]


def series(n, step, drift, end):
    t0 = (end // step) * step - (n - 1) * step
    c = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.012 if step == DAY else 0.004, n)))
    rows, prev = [], c[0]
    for i, x in enumerate(c):
        rows.append([t0 + i * step, prev, x, max(prev, x) * 1.006, min(prev, x) * 0.994, 1000 + rng.random() * 500])
        prev = x
    return rows


def fake_get(url, params=None, timeout=10, retries=3):
    if "ticker/ALL_KRW" in url:
        data = {tk: {"closing_price": "1000", "fluctate_rate_24H": "1.2", "acc_trade_value_24H": "3e10"} for tk in TK}
        data["date"] = "x"
        return {"status": "0000", "data": data}
    if "candlestick" in url:
        iv = url.rsplit("/", 1)[1]
        step = DAY if iv == "24h" else HOUR
        return {"status": "0000", "data": series(300 if step == DAY else 120, step, 0.004 if step == DAY else 0.0, now)}
    return None


bot.http_json = fake_get
sent = []
bot.slack_post = lambda text, blocks: sent.append(text) or True
bot.fetch_taker_ratio = lambda: 0.52

with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    latest = bot.run_scan()
    for f in ("latest.json", "ledger.json", "stats.json"):
        assert os.path.exists(os.path.join(td, f)), f
        json.load(open(os.path.join(td, f)))
    print("market:", latest["market"]["gate"], "| picks:", len(latest["picks"]), "| watch:", len(latest["watch"]))
    assert "USDT" not in [p["ticker"] for p in latest["picks"]]
    bot.run_track()
    bot.run_scan()  # 두 번째 스캔: 계획 숫자가 유지되어야 함
    led = json.load(open(os.path.join(td, "ledger.json")))
    print("ledger entries:", len(led))
    ids = [e["id"] for e in led]
    assert len(ids) == len(set(ids))
    print("slack posts:", len(sent))
print("PIPELINE OK")

# --- 추천 경로 강제 테스트 ---
def fake_pb(ctx, rs7):
    if ctx["ticker"] not in ("SUI", "HBAR"):
        return None
    cur = ctx["cur"]
    plan = bot.make_plan(cur, cur * 0.95, "now", cur * 0.99, cur * 1.005)
    return {"setup": "pullback", "state": "now", "plan": plan, "score": 70, "reasons": ["a", "b", "c"], "risks": [],
            "trigger_text": "", "trigger_price": plan["zone_hi"], "metrics": {}}
bot.detect_pullback = fake_pb
bot.detect_breakout_retest = lambda c, r: None
bot.ai_veto = lambda picks: ({"SUI": {"verdict": "block", "note": "언락", "positive": None},
                              "HBAR": {"verdict": "caution", "note": "소송", "positive": None}}, "ok")
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    bot.compute_market = lambda c, t: {"gate": "GO", "label": "매수 가능", "size_mult": 1.0, "checks": [], "metrics": {}, "headline": "t"}
    L = bot.run_scan()
    assert [p["ticker"] for p in L["picks"]] == ["HBAR"], L["picks"]
    assert L["excluded"] and L["excluded"][0]["ticker"] == "SUI"
    p1 = L["picks"][0]
    L2 = bot.run_scan()
    assert L2["picks"][0]["entry"] == p1["entry"] and L2["picks"][0]["carried"], "계획 고정 실패"
    led = json.load(open(os.path.join(td, "ledger.json")))
    assert len(led) == 1
    print("PICK PATH OK", p1["entry"], p1["stop"], p1["t1"], p1["t2"])
