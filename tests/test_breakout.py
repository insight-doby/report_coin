"""돌파 추천 테스트 — 신호 감지, '샀다면' 재생(절반 → +3% 추가 → 평단 손절 → +20% 절반 → 10일선), 알림."""
import sys, os, tempfile, json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import bot

D, H = bot.DAY_MS, bot.HOUR_MS
T0 = datetime(2026, 6, 1, tzinfo=bot.KST).timestamp() * 1000
CFG = dict(bot.BRK_CFG_DEFAULT)


def mk(c, step, t0=T0, v=None):
    c = np.array(c, float); o = np.r_[c[0], c[:-1]]
    return {"t": (t0 + np.arange(len(c)) * step).astype(float), "o": o, "h": np.maximum(o, c) * 1.002,
            "l": np.minimum(o, c) * 0.998, "c": c, "v": np.array(v if v is not None else [1000] * len(c), float)}


def test_daily_signal():
    c = list(np.linspace(150, 100, 140)) + [99, 98, 97, 115]          # 하락 후 마지막 날 20일선 위로
    d = mk(c, D)
    assert bot.brk_daily_signal(d, len(c) - 1) == "brk_ma20"
    assert bot.brk_daily_signal(d, len(c) - 2) is None
    # 볼린저 수축 후 상단 돌파 (20일선은 이미 위)
    c2 = list(np.linspace(80, 100, 120)) + [100 + 0.05 * (i % 3) for i in range(30)] + [104]
    assert bot.brk_daily_signal(mk(c2, D), len(c2) - 1) == "brk_squeeze"


def test_4h_signal():
    c = [100] * 25 + [101, 106]
    v = [1000] * 26 + [4000]
    h4 = mk(c, 4 * H, v=v)
    assert bot.brk_4h_signal(h4, 26, 103.0, 3.0) == 4.0
    v2 = [1000] * 26 + [2000]
    assert bot.brk_4h_signal(mk(c, 4 * H, v=v2), 26, 103.0, 3.0) is None       # 거래량 부족
    assert bot.brk_4h_signal(h4, 26, 107.0, 3.0) is None                      # 20일선 못 넘음


def entry(price=100.0):
    return bot.brk_new_entry("TST", "brk_ma20", price, T0, "GO", CFG, 95.0)


def hourly(path):
    """path: [(open, high, low, close)] 1시간봉."""
    a = np.array(path, float)
    t = (T0 + H + np.arange(len(a)) * H).astype(float)
    return {"t": t, "o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3]}


def test_replay_first_stop():
    e = entry()
    h1 = hourly([(100, 101, 99, 100), (100, 100, 92, 93)])        # 추가 매수 전 -7% 손절
    bot.brk_replay(e, h1, {}, CFG, h1["t"][-1] + 2 * H)
    assert e["status"] == "closed" and e["exit_reason"] == "stop"
    assert abs(e["ret_pct"] - 0.5 * (-7 - bot.BRK_FEE)) < 0.01, e["ret_pct"]


def test_replay_add_then_avg_stop():
    e = entry()
    h1 = hourly([(100, 103.5, 100, 103), (103, 103, 101.4, 101.6)])  # +3% 추가 → 다음 봉 평단(101.5) 손절
    bot.brk_replay(e, h1, {}, CFG, h1["t"][-1] + 2 * H)
    assert e["added"] and e["exit_reason"] == "avg_stop", e
    exp = 0.5 * ((101.5 / 100 - 1) * 100 - bot.BRK_FEE) + 0.5 * ((101.5 / 103 - 1) * 100 - bot.BRK_FEE)
    assert abs(e["ret_pct"] - exp) < 0.01, (e["ret_pct"], exp)


def test_replay_half_then_ma_exit():
    e = entry()
    path = [(100, 103.5, 100, 103), (103, 121, 103, 120)] + [(120, 121, 118, 119)] * 22
    h1 = hourly(path)
    # 일봉 종가: 10일선 계산용 (마지막 날 종가가 10일선 아래)
    day = datetime.fromtimestamp(h1["t"][-1] / 1000, bot.KST).strftime("%Y-%m-%d")
    dc = {f"2026-05-{d:02d}": 130.0 for d in range(20, 30)}
    dc[day] = 119.0
    dcs = dict(sorted(dc.items()))
    # 마지막 봉을 23시로 맞춤
    last = datetime.fromtimestamp(h1["t"][-1] / 1000, bot.KST)
    shift = (23 - last.hour) * H
    h1["t"] = h1["t"] + shift
    day = datetime.fromtimestamp(h1["t"][-1] / 1000, bot.KST).strftime("%Y-%m-%d")
    dcs = {k: v for k, v in dcs.items() if k < day}; dcs[day] = 119.0
    bot.brk_replay(e, h1, dict(sorted(dcs.items())), CFG, h1["t"][-1] + 2 * H)
    assert e["added"] and e["half"] and e["exit_reason"] == "ma_exit", e
    half = 0.5 * 0.5 * ((120 / 100 - 1) * 100 - bot.BRK_FEE) + 0.5 * 0.5 * ((120 / 103 - 1) * 100 - bot.BRK_FEE)
    rest = 0.25 * ((119 / 100 - 1) * 100 - bot.BRK_FEE) + 0.25 * ((119 / 103 - 1) * 100 - bot.BRK_FEE)
    assert abs(e["ret_pct"] - (half + rest)) < 0.05, (e["ret_pct"], half + rest)
    txt = bot.brk_result_text(e)
    assert "📗" in txt and "+3% 추가 매수" in txt and "+20% 절반 익절" in txt and "10일선" in txt, txt


def test_alert_text_and_stats():
    e = entry(1850)
    txt = bot.brk_alert_text(e, "GO", CFG)
    assert "1,850원" in txt and "1,906원" in txt and ("1,720원" in txt or "1,721원" in txt), txt
    assert "2,220원" in txt and "10일선" in txt and "계좌 비서" in txt
    small = bot.brk_new_entry("DEEP", "brk_4h", 21.0, T0, "GO", CFG, 20.0, 3.4)
    assert "1/4" in bot.brk_alert_text(small, "GO", CFG) and "거래대금 3.4배" in bot.brk_alert_text(small, "GO", CFG)
    a, b = entry(), entry()
    a.update(status="closed", ret_pct=3.0); b.update(status="closed", ret_pct=-1.0)
    s = bot.brk_stats([a, b])
    assert s["overall"]["n_closed"] == 2 and s["overall"]["win_rate"] == 50.0 and s["overall"]["avg_pct"] == 1.0


def test_run_breakout(monkeypatch=None):
    tmp = tempfile.mkdtemp()
    bot.LOCAL_DATA_DIR = tmp
    store = bot.Store()
    store.write("brk_cfg.json", {"swing": ["AAA"], "large": [], "small": []}, "t")
    at = T0 + 160 * D + 30 * 60 * 1000                                  # 160일째 00:30
    closes = list(np.linspace(150, 100, 158)) + [99, 115]               # 어제 20일선 돌파
    def fake(tk, tf, count=60):
        if tf == "1d":
            return mk(closes + [116], D)
        if tf == "4h":
            return mk([100] * 40, 4 * H, t0=at - 41 * 4 * H)
        if tf == "1h":
            return mk([116, 116.5], H, t0=at - 2 * H)
    bot.fetch_v1 = fake
    sent = []
    bot.telegram_send = lambda t, html=False: sent.append(t) or True
    fresh = bot.run_breakout(store, at, "GO")
    assert len(fresh) == 1 and fresh[0]["setup"] == "brk_ma20", fresh
    assert "돌파 추천" in sent[0]
    led = json.load(open(os.path.join(tmp, "brk_ledger.json")))
    assert led[0]["ticker"] == "AAA" and led[0]["status"] == "open"
    # 다시 돌려도 중복 추천 없음
    assert bot.run_breakout(store, at + 10 * 60 * 1000, "GO") == []
    # 매수 금지면 기록만, 알림 없음
    tmp2 = tempfile.mkdtemp(); bot.LOCAL_DATA_DIR = tmp2; store2 = bot.Store()
    store2.write("brk_cfg.json", {"swing": ["AAA"], "large": [], "small": []}, "t")
    sent.clear()
    assert bot.run_breakout(store2, at, "STOP") == [] and not sent
    assert json.load(open(os.path.join(tmp2, "brk_ledger.json")))[0]["gate"] == "STOP"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
