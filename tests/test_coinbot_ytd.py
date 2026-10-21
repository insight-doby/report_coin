"""올해 실질 수익 역산 테스트 — 1/1 자산을 체결·입출금으로 거꾸로 계산해 실제 번 돈만 나오는지."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.update(BITHUMB_ACCESS_KEY="a", BITHUMB_SECRET_KEY="b", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
import coinbot as c

NOW = c.datetime(2026, 10, 21, 18, 0, tzinfo=c.KST).timestamp()
c.now_ts = lambda: NOW
iso = lambda y, m, d: c.datetime(y, m, d, 12, 0, tzinfo=c.KST).isoformat()

ORDERS = {  # 올해: AAVE 1개 매수, LINK 50개 매도 / 작년: LINK 100개 매수
    "o_old": dict(market="KRW-LINK", side="bid", vol=100, px=15000, fee=600, at=iso(2025, 12, 1)),
    "o1": dict(market="KRW-AAVE", side="bid", vol=1, px=200000, fee=80, at=iso(2026, 3, 1)),
    "o2": dict(market="KRW-LINK", side="ask", vol=50, px=25000, fee=500, at=iso(2026, 7, 1)),
}
DEP = [{"uuid": "d1", "state": "ACCEPTED", "currency": "KRW", "amount": "3000000", "done_at": iso(2026, 6, 1)},
       {"uuid": "d0", "state": "ACCEPTED", "currency": "KRW", "amount": "9000000", "done_at": iso(2025, 6, 1)}]
WD = [{"uuid": "w1", "state": "DONE", "currency": "KRW", "amount": "1000000", "fee": "1000", "done_at": iso(2026, 8, 1)}]


def fake(path, params=None, private=True):
    params = params or {}
    if path == "/v1/accounts":
        return [{"currency": "KRW", "balance": "8048420", "locked": "0", "avg_buy_price": "0"},
                {"currency": "LINK", "balance": "50", "locked": "0", "avg_buy_price": "15000"},
                {"currency": "AAVE", "balance": "1", "locked": "0", "avg_buy_price": "200080"}]
    if path == "/v1/orders":
        if params.get("page", 1) > 1:
            return []
        return [{"uuid": u, "executed_volume": str(o["vol"]), "created_at": o["at"]} for u, o in ORDERS.items()]
    if path == "/v1/order":
        o = ORDERS[params["uuid"]]
        return {"uuid": params["uuid"], "market": o["market"], "side": o["side"], "state": "done",
                "executed_volume": str(o["vol"]), "paid_fee": str(o["fee"]), "created_at": o["at"],
                "trades": [{"price": str(o["px"]), "volume": str(o["vol"]), "funds": str(o["px"] * o["vol"]),
                            "created_at": o["at"]}]}
    if path == "/v1/deposits/krw":
        return DEP if params.get("page", 1) == 1 else []
    if path == "/v1/withdraws/krw":
        return WD if params.get("page", 1) == 1 else []
    if path in ("/v1/deposits", "/v1/withdraws"):
        return []
    if path == "/v1/candles/days":
        assert params["to"] == "2026-01-01T00:00:00", params
        return [{"trade_price": {"KRW-LINK": 20000, "KRW-AAVE": 150000}[params["market"]]}]
    if path == "/v1/market/all":
        return [{"market": "KRW-LINK"}, {"market": "KRW-AAVE"}]
    if path == "/v1/ticker":
        px = {"LINK": 18000, "AAVE": 210000}
        return [{"market": m, "trade_price": px[m.split("-")[1]]} for m in params["markets"].split(",")]
    raise AssertionError(path)


HONOR_ASC = {"on": True}


def fake_sorted(path, params=None, private=True):
    data = fake(path, params, private)
    key = lambda x: c.parse_ts(x.get("done_at") or x.get("created_at"))
    if isinstance(data, list) and params and params.get("order_by") and data and isinstance(data[0], dict) \
            and (data[0].get("created_at") or data[0].get("done_at")):
        data = sorted(data, key=key, reverse=params["order_by"] == "desc" or not HONOR_ASC["on"])
        if params.get("limit"):
            data = data[:params["limit"]]
    return data


c.bget = fake_sorted


def test_ytd():
    bot = c.Bot(c.DB(os.path.join(tempfile.mkdtemp(), "y.db")))
    st = bot.state()
    r = bot.compute_ytd()
    # 1/1: 현금 5,000,000 + LINK 100 × 20,000 = 7,000,000
    assert abs(r["e0"] - 7_000_000) < 1, r["e0"]
    y = bot.ytd(st)
    # 지금 9,158,420 − 1/1 7,000,000 − 순입금 2,000,000 = 158,420 (LINK +149,500, AAVE +9,920, 출금수수료 −1,000)
    assert abs(y["pnl"] - 158_420) < 1, y["pnl"]
    assert abs(y["net"] - 2_000_000) < 1 and not y["warn"], y
    assert 1.5 < y["ret"] < 2.5, y["ret"]
    txt = bot.block_ytd(st)
    assert "2026년 실질 수익" in txt and "+15.8만원" in txt, txt
    print(txt)


def test_history():
    bot = c.Bot(c.DB(os.path.join(tempfile.mkdtemp(), "h.db")))
    txt = bot.block_history()
    HONOR_ASC["on"] = False                                  # 빗썸이 오래된 순 정렬을 무시해도 같은 결과
    assert bot.block_history() == txt
    assert "체결: <b>2025-12-01</b>" in txt and "원화 입금: <b>2025-06-01</b>" in txt and "년치" in txt, txt
    assert bot.handle.__func__  # 명령어 매칭
    print(txt)


if __name__ == "__main__":
    test_ytd()
    test_history()
    print("OK")
