"""호가 기록 테스트 — 저장, 날짜 바뀔 때 업로드, 기간 끝 알림."""
import os, sys, tempfile, json, gzip
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import coinbot as c


def fake_get(path, q):
    if path == "/v1/orderbook":
        return [{"market": m, "orderbook_units": [{"bid_price": 100 - k, "bid_size": 1, "ask_price": 101 + k, "ask_size": 2}
                                                  for k in range(5)]} for m in q["markets"].split(",")]
    if path == "/v1/trades/ticks":
        return [{"sequential_id": i, "timestamp": fake_get.t * 1000, "trade_price": 100, "trade_volume": 1, "ask_bid": "ASK"}
                for i in range(3)]


def test_mmrec():
    put, sent = {}, []
    r = c.MMRecorder(path=os.path.join(tempfile.mkdtemp(), "m.db"), markets=["KRW-XRP", "KRW-ETH"], days=1,
                     getter=fake_get, putter=lambda p, raw, msg: put.__setitem__(p, raw) or True, sender=sent.append)
    db = r._db()
    t0 = c.datetime(2026, 11, 11, 23, 59, 50, tzinfo=c.KST).timestamp()
    fake_get.t = t0
    st = {"next_ob": 0, "next_tr": 0, "day": "2026-11-11", "start": t0}
    r.step(db, t0, st)
    assert db.execute("SELECT COUNT(*) FROM ob").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM tr").fetchone()[0] == 6
    r.record_tr(db)                                                   # 중복 체결은 다시 안 쌓임
    assert db.execute("SELECT COUNT(*) FROM tr").fetchone()[0] == 6
    r.step(db, t0 + 20, st)                                           # 자정 넘김 → 11일치 업로드
    assert "data/mmrec/2026-11-11_XRP.json.gz" in put and "data/mmrec/status.json" in put, put.keys()
    doc = json.loads(gzip.decompress(put["data/mmrec/2026-11-11_XRP.json.gz"]))
    assert doc["ob"][0][1] == 100 and doc["ob"][0][3] == 101 and len(doc["ob"][0]) == 13 and len(doc["tr"]) == 3
    r.finish(db, st)
    assert "호가 기록 끝" in sent[-1] and json.loads(put["data/mmrec/status.json"])["finished"]


if __name__ == "__main__":
    test_mmrec(); print("OK")
