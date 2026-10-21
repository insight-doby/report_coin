import sys, os, tempfile, json, io, contextlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
bot.BITHUMB_ACCESS_KEY, bot.BITHUMB_SECRET_KEY = "a", "b"
class R:
    status_code = 200
    def json(self): return [{"currency": "KRW", "balance": "1234567", "locked": "0", "avg_buy_price": "0"},
                            {"currency": "AAVE", "balance": "10", "locked": "0", "avg_buy_price": "199700"},
                            {"currency": "NEW", "balance": "1000", "locked": "0", "avg_buy_price": "50"},
                            {"currency": "DUST", "balance": "1", "locked": "0", "avg_buy_price": "5"}]
calls = []
bot.requests.get = lambda url, headers=None, timeout=None: calls.append(headers) or R()
tg = []
bot.telegram_send = lambda t, html=False: tg.append(t) or True
tick = {"AAVE": {"price": 207000.0}, "NEW": {"price": 60.0}, "DUST": {"price": 6.0}}
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    json.dump({"positions": [{"ticker": "AAVE", "avg": 1, "style": "swing", "t1": 215000}, {"ticker": "ADA", "avg": 322.3, "style": "exit"}]},
              open(os.path.join(td, "positions.json"), "w"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        snap = bot.sync_positions(bot.Store(), 0, tick)
    saved = open(os.path.join(td, "positions.json")).read()
    for secret in ("1234567", "2070000", "\"qty\"", "\"value\""):          # 수량·금액은 공개 파일/로그에 없어야 함
        assert secret not in saved and secret not in out.getvalue(), secret
    P = {p["ticker"]: p for p in json.loads(saved)["positions"]}
    assert set(P) == {"AAVE", "NEW"} and P["AAVE"]["t1"] == 215000 and P["AAVE"]["avg"] == 199700
    assert calls[0]["Authorization"].startswith("Bearer ") and calls[0]["Authorization"].count(".") == 2
    assert any("NEW" in t for t in tg) and any("ADA" in t for t in tg)
    bot.telegram_portfolio(snap)
    assert "총자산" in tg[-1]
print("ACCOUNT OK")
