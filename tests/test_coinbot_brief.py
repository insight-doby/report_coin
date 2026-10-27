"""아침 브리핑 테스트 — 시장·보유 코인 계획선·밤사이 체결/알림이 들어가는지."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import test_coinbot_ytd as T
c = T.c

ALERTS = {"alerts": [
    {"ticker": "LINK", "cond": "low_below", "price": 17000, "status": "active"},
    {"ticker": "LINK", "cond": "high_above", "price": 19000, "status": "active"},
    {"ticker": "LINK", "cond": "close_below", "price": 17500, "status": "active"},
    {"ticker": "AAVE", "cond": "low_below", "price": 190000, "status": "fired", "fired_at": "2026-10-21T23:00:00+09:00"},
]}
LATEST = {"market": {"gate": "STOP", "metrics": {"breadth_pct": 26.0}}}


class R:
    def __init__(self, d): self.d = d
    def json(self): return self.d


def fake_get(url, timeout=10):
    return R(ALERTS if url.endswith("price_alerts.json") else LATEST if url.endswith("latest.json") else {})


def bget(path, params=None, private=True):
    if path == "/v1/candles/minutes/60":
        return [{"candle_date_time_kst": "2026-10-20T22:00:00", "opening_price": {"KRW-BTC": 90000000}.get(params["market"], 3000000)}]
    if path == "/v1/candles/days" and params.get("count") == 27:
        base = {"KRW-BTC": 100000000}.get(params["market"], 20000)
        return [{"candle_date_time_kst": f"2026-09-{i + 1:02d}T00:00:00", "trade_price": base * (1.3 - i * 0.01)} for i in range(27)]
    if path == "/v1/candles/minutes/240":
        base = {"KRW-BTC": 100000000}.get(params["market"], 20000)
        return [{"candle_date_time_kst": f"2026-10-20T{i:02d}:00:00", "trade_price": base} for i in range(21)]
    if path == "/v1/ticker" and "KRW-BTC" in params["markets"]:
        return [{"market": m, "trade_price": {"BTC": 99000000, "ETH": 3000000}.get(m[4:], 18000)} for m in params["markets"].split(",")]
    return T.fake_sorted(path, params, private)


def test_brief():
    c.bget = bget
    c.requests.get = fake_get
    c.krw_markets = lambda: {"BTC", "ETH", "LINK", "AAVE"}
    c.MAJOR_KRW = 500000
    bot = c.Bot(c.DB(os.path.join(tempfile.mkdtemp(), "b.db")))
    st = bot.snapshot(notify=False)
    txt = bot.morning_brief(st)
    print(txt)
    assert "아침 브리핑" in txt and "🔴 매수 금지" in txt and "알트 26%만" in txt
    assert "비트 9,900만" in txt and "밤사이 +10.0%" in txt
    assert "추매 17,000 -6%" in txt and "매도 19,000 +6%" in txt and "정리 17,500 -3%" in txt
    assert "📉 하락 추세 · 20일선 22,900 (-9.2%) · 4시간 반등 중" in txt
    assert "⚠️ LINK" in txt and "울린 알림: AAVE 190,000" in txt
    bot.handle("아침 브리핑")
    assert "아침 브리핑" in T.SENT[-1]


if __name__ == "__main__":
    test_brief()
    print("OK")
