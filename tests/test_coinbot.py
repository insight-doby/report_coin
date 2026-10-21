"""서버용 계좌 비서(coinbot) 모의 테스트 — 실제 빗썸/텔레그램 호출 없음."""
import os, sys, tempfile, importlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.update(BITHUMB_ACCESS_KEY="a", BITHUMB_SECRET_KEY="b", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
import coinbot
importlib.reload(coinbot)

T0 = 1792500000.0
clock = {"t": T0}
coinbot.now_ts = lambda: clock["t"]
sent = []
coinbot.send = lambda txt: sent.append(txt)
coinbot.requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net"))

ex = {"cash": 1_000_000.0, "hold": {"LINK": [10.0, 17000.0]}, "prices": {"LINK": 17000.0, "AAVE": 200000.0},
      "wait": [], "done": [], "details": {}, "dep_krw": []}


def iso(t):
    return coinbot.kst(t).isoformat()


def fake_bget(path, params=None, private=True):
    if path == "/v1/accounts":
        out = [{"currency": "KRW", "balance": str(ex["cash"]), "locked": "0", "avg_buy_price": "0"}]
        out += [{"currency": c, "balance": str(q), "locked": "0", "avg_buy_price": str(a)} for c, (q, a) in ex["hold"].items()]
        return out
    if path == "/v1/orders":
        return ex["wait"] if params.get("state") == "wait" else (ex["done"] if params.get("page") == 1 else [])
    if path == "/v1/order":
        return ex["details"][params["uuid"]]
    if path == "/v1/deposits/krw":
        return ex["dep_krw"]
    if path in ("/v1/withdraws/krw", "/v1/deposits", "/v1/withdraws"):
        return []
    raise AssertionError(path)


coinbot.bget = fake_bget
coinbot.fetch_prices = lambda coins: {c: ex["prices"][c] for c in coins if c in ex["prices"]}


def fill(u, side, coin, px, vol, t, fee_rate=0.0004):
    funds = px * vol
    ex["done"].insert(0, {"uuid": u, "executed_volume": str(vol)})
    ex["details"][u] = {"uuid": u, "market": f"KRW-{coin}", "side": side, "state": "done", "executed_volume": str(vol),
                        "paid_fee": str(funds * fee_rate), "created_at": iso(t),
                        "trades": [{"price": str(px), "volume": str(vol), "funds": str(funds), "created_at": iso(t)}]}


def test_flow():
    db = coinbot.DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    bot = coinbot.Bot(db)
    # 과거 체결(설치 전): LINK 10개 17000에 매수
    fill("o1", "bid", "LINK", 17000, 10, T0 - 86400 * 3)
    bot.snapshot(notify=False)
    db.x("DELETE FROM pos")
    bot.sync_fills(notify=False, backfill=True)
    bot.snapshot(notify=False)
    assert abs(bot._pos("LINK")["qty"] - 10) < 1e-9, bot._pos("LINK")
    assert not sent, "백필은 알림 없음"

    # 1시간 뒤: AAVE 1개 200,000 매수 (현금 감소)
    clock["t"] += 3600
    fill("o2", "bid", "AAVE", 200000, 1, clock["t"] - 60)
    ex["cash"] -= 200000 * 1.0004
    ex["hold"]["AAVE"] = [1.0, 200080.0]
    bot.sync_fills()
    assert any("매수 체결 · AAVE" in s for s in sent)

    # 2일 뒤: AAVE 210,000 전량 매도 → +약 4.9%
    clock["t"] += 86400 * 2
    ex["prices"]["AAVE"] = 210000.0
    fill("o3", "ask", "AAVE", 210000, 1, clock["t"] - 60)
    ex["cash"] += 210000 * 0.9996
    del ex["hold"]["AAVE"]
    bot.sync_fills()
    tr = db.q("SELECT * FROM trades WHERE uuid='o3'")[0]
    assert 4.5 < tr["pct"] < 5.0, tr["pct"]
    assert 1.9 < tr["hold_h"] / 24 < 2.1
    bot.snapshot()

    # 입금 100만원 → 수익률에서 제외
    clock["t"] += 600
    ex["cash"] += 1_000_000
    ex["dep_krw"] = [{"uuid": "d1", "state": "ACCEPTED", "currency": "KRW", "amount": "1000000",
                      "done_at": iso(clock["t"] - 30)}]
    bot.snapshot()
    curve, flows = bot.curve()
    p = bot.period(0, curve, flows)
    exp = 210000 * 0.9996 - 200000 * 1.0004          # 실제로 번 돈만
    assert abs(p["pnl"] - exp) < 1, (p["pnl"], exp)
    assert any("입금 확인" in s for s in sent)

    rep = bot.full_report(db.get("last_state"))
    for key in ("총자산", "기간별 손익", "보유 코인", "목표", "매매 성적", "승률 100%"):
        assert key in rep, key
    print(rep)
    print("----")
    bot.handle("보유"); bot.handle("/trades"); bot.handle("주문"); bot.handle("목표")
    print("\n----\n".join(sent[-4:]))


if __name__ == "__main__":
    test_flow()
    print("\nOK")
