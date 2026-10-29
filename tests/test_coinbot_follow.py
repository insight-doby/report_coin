"""추천 따라 산 매매 테스트 — 체결과 추천 연결, 금액 안내, 단계 안내, 성적 비교."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import test_coinbot_ytd as T
c = T.c

NOW = T.NOW
SIG = {"id": "LINK-brk_ma20-1", "kind": "brk", "ticker": "LINK", "setup": "brk_ma20", "tier": "swing", "gate": "GO",
       "issued_ts": (NOW - 3600) * 1000, "status": "open", "entry": 18000.0, "add_px": 18540.0, "stop0": 16740.0,
       "tp_px": 21600.0}
PAPER = dict(SIG, id="SOL-brk_ma20-0", ticker="SOL", status="closed", issued_ts=(NOW - 5 * 86400) * 1000,
             exit_ts=(NOW - 86400) * 1000, ret_pct=4.0)
PUB = {"brk_ledger.json": [SIG, PAPER], "ledger.json": []}


def bget(path, params=None, private=True):
    if path == "/v1/candles/days" and params.get("count") == 13:
        cl = [20000] * 10 + [18800, 19000, 19500]              # 이틀 연속 10일선 아래 마감 (마지막은 진행 중)
        return [{"candle_date_time_kst": f"2026-10-{i + 10:02d}T00:00:00", "trade_price": v} for i, v in enumerate(cl)]
    return T.fake_sorted(path, params, private)


def test_follow_flow():
    c.bget = bget
    sent = []
    c.send = lambda t: sent.append(t)
    bot = c.Bot(c.DB(os.path.join(tempfile.mkdtemp(), "f.db")))
    bot.pub = lambda name, ttl=600: PUB.get(name)
    st = bot.state()
    # 1) 새 추천 금액 안내
    bot.announce_signals(st)
    assert sent and "돌파 추천 금액 · LINK" in sent[-1] and "계획 금액" in sent[-1], sent
    n = len(sent); bot.announce_signals(st); assert len(sent) == n          # 한 번만
    # 2) 추천 1시간 뒤 매수 → 추천 따라 산 매매로 기록
    bot.apply_fill({"uuid": "b1", "coin": "LINK", "side": "bid", "volume": 10, "funds": 180000, "fee": 72, "ts": NOW})
    assert "돌파 추천을 따라 산 매매" in sent[-1] and "18,540원" in sent[-1], sent[-1]
    # 3) +3%에서 2차 매수 → 손절을 평단으로
    bot.apply_fill({"uuid": "b2", "coin": "LINK", "side": "bid", "volume": 10, "funds": 185400, "fee": 74, "ts": NOW + 600})
    assert "손절을 평단으로" in sent[-1], sent[-1]
    # 4) 단계 안내: +20% 도달 + 10일선 이탈
    st["hold"]["LINK"] = dict(st["hold"]["LINK"], price=21700.0, avg=18270.0)
    bot.watch_follow(st)
    assert "+20% 도달" in sent[-1] and "이틀 연속 10일선" in sent[-1], sent[-1]
    k = len(sent); bot.watch_follow(st); assert len(sent) == k                # 같은 안내는 한 번만
    # 5) 전량 매도 → 결과 기록
    p = bot._pos("LINK")
    bot.apply_fill({"uuid": "s1", "coin": "LINK", "side": "ask", "volume": p["qty"], "funds": p["qty"] * 21000,
                    "fee": 100, "ts": NOW + 7200})
    assert "추천 따라 한 매매 끝" in sent[-1], sent[-1]
    done = bot.db.get("follow_done")
    assert len(done) == 1 and done[0]["kind"] == "brk" and done[0]["pnl"] > 0, done
    # 6) 추천 성적
    txt = bot.block_signals(0, "전체 기간")
    print(txt)
    assert "돌파 추천" in txt and "1건 +4.0%" in txt and "추천 밖" in txt
    # 추천과 상관없는 매수는 연결 안 됨
    bot.apply_fill({"uuid": "b9", "coin": "AAVE", "side": "bid", "volume": 1, "funds": 200000, "fee": 80, "ts": NOW})
    assert "따라 산 매매" not in sent[-1]


if __name__ == "__main__":
    test_follow_flow()
    print("OK")
