"""웹소켓 호가·체결 기록기 테스트 — 프레임 해석(조각·긴 길이·핑·타임아웃), 파일 기록, 구간 넘김, 업로드, 끝까지 한 바퀴."""
import gzip, json, os, socket, struct, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import coinbot as c


def frame(op, payload=b"", fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class FakeSock:
    """정해 둔 조각을 순서대로 돌려준다. None 은 타임아웃."""
    def __init__(self, chunks):
        self.chunks, self.sent = list(chunks), []

    def recv(self, n):
        if not self.chunks:
            return b""
        x = self.chunks.pop(0)
        if x is None:
            raise socket.timeout()
        return x

    def sendall(self, b):
        self.sent.append(bytes(b))

    def close(self):
        pass


def unmask(sent):
    op, ln, pos = sent[0] & 0x0F, sent[1] & 0x7F, 2
    assert sent[1] & 0x80, "클라이언트 프레임은 반드시 마스크"
    if ln == 126:
        ln, pos = struct.unpack(">H", sent[2:4])[0], 4
    elif ln == 127:
        ln, pos = struct.unpack(">Q", sent[2:10])[0], 10
    mk = sent[pos:pos + 4]
    return op, bytes(b ^ mk[i % 4] for i, b in enumerate(sent[pos + 4:pos + 4 + ln]))


def test_miniws_frames():
    big, huge = b"x" * 300, b"y" * 70000
    stream = (frame(0x1, b"hello") + frame(0x9, b"pp") + frame(0x1, b"ab", fin=False) + frame(0x0, b"cd") +
              frame(0x2, big) + frame(0x2, huge) + frame(0xA, b"") + frame(0x1, b"last") + frame(0x8, b""))
    chunks = [stream[i:i + 7] for i in range(0, 40, 7)] + [None] + [stream[42:45], None, stream[45:]]   # 프레임 한가운데서 타임아웃 2번
    ws = c.MiniWS("wss://example.invalid/ws")
    ws.sock = FakeSock(chunks)
    got, timeouts = [], 0
    while True:
        try:
            got.append(ws.recv())
        except socket.timeout:
            timeouts += 1
        except ConnectionError:
            break
    assert got == [b"hello", b"abcd", big, huge, b"last"], [g[:10] for g in got]
    assert timeouts == 2
    assert unmask(ws.sock.sent[0]) == (0xA, b"pp")                 # 핑에는 같은 내용으로 퐁
    for payload in (b"hi", b"z" * 200, b"w" * 70000):               # 보내는 프레임: 길이 3종
        ws.sock.sent.clear(); ws._send(0x1, payload)
        assert unmask(ws.sock.sent[0]) == (0x1, payload)


TR = {"type": "trade", "code": "KRW-AAA", "trade_price": 100.5, "trade_volume": 4e-05, "ask_bid": "ASK", "trade_timestamp": 1794379308780,
      "sequential_id": 1222418451361219171, "timestamp": 1794379308983, "stream_type": "REALTIME"}
OB = {"type": "orderbook", "code": "KRW-AAA", "total_ask_size": 10.0, "total_bid_size": 20.0, "timestamp": 1794379309942322, "stream_type": "REALTIME",
      "orderbook_units": [{"ask_price": 101 + k, "bid_price": 100 - k, "ask_size": 1.5, "bid_size": 2.5} for k in range(15)]}


def mk(tmp, **kw):
    sent, puts, ups = [], {}, []
    def uploader(path, name):
        ups.append((name, gzip.open(path, "rt").read()))
        return kw.get("http", 201)
    r = c.MMWS(base=tmp, days=kw.get("days", 1), markets=["KRW-AAA", "KRW-BBB"], uploader=uploader,
               status_put=lambda p, raw, msg: puts.__setitem__(p, raw) or True, sender=sent.append, ws_factory=kw.get("ws_factory"))
    return r, sent, puts, ups


def test_record_rotate_upload():
    tmp = tempfile.mkdtemp()
    r, sent, puts, ups = mk(tmp)
    r.load_state(); r.st.update(start=time.time(), markets=["KRW-AAA", "KRW-BBB"])
    t1 = c.datetime(2026, 11, 11, 5, 59, 59, tzinfo=c.timezone.utc).timestamp()
    r.on_message(json.dumps(TR).encode(), t1)
    r.on_message(json.dumps(OB).encode(), t1 + 0.5)
    r.on_message(b'{"status":"UP"}', t1 + 0.6)                      # 무시
    r.on_message(b"not json", t1 + 0.7)                              # 오류로만 셈
    r.on_message(json.dumps(dict(TR, sequential_id=7)).encode(), t1 + 2)    # 06시 구간으로 넘어감
    for _, f in r.w.values():
        f.flush()
    a = open(os.path.join(tmp, "AAA_20261111T00.csv")).read().splitlines()
    assert a[0] == f"T,{int(t1 * 1e6)},1794379308983,1794379308780,1222418451361219171,ASK,100.5,4e-05,REALTIME", a[0]
    b = a[1].split(",")
    assert b[0] == "B" and b[2] == "1794379309942322" and b[4:6] == ["10.0", "20.0"] and len(b) == 6 + 4 * 5 and b[6:10] == ["100", "2.5", "101", "1.5"], b
    assert r.st["counts"]["KRW-AAA"] == [2, 1] and r.session["errors"] == {"parse": 1}
    assert abs(r.lat["trade_pub_ms"][0] - 203) < 1e-6
    r.scan_closed(t1 + 2)                                            # 지난 구간(00시 파일)만 대기열로
    assert [os.path.basename(p) for p in r.upq] == ["AAA_20261111T00.csv"]
    r.scan_closed(t1 + 2); assert len(r.upq) == 1                    # 두 번 넣지 않음
    r.stop_up.set(); r.upload_loop()
    assert ups[0][0] == "AAA_20261111T00.csv.gz" and ups[0][1].count("\n") == 2
    assert not os.path.exists(os.path.join(tmp, "AAA_20261111T00.csv")) and not os.path.exists(os.path.join(tmp, "AAA_20261111T00.csv.gz"))
    assert r.st["uploaded"][0][0] == "AAA_20261111T00.csv.gz" and os.path.exists(os.path.join(tmp, "AAA_20261111T06.csv"))
    st = r.status()
    assert st["uploaded_files"] == 1 and st["pending_files"] == 1 and st["latency_ms"]["book_ms"]["n"] == 1


def test_upload_failure_keeps_file():
    tmp = tempfile.mkdtemp()
    r, sent, puts, ups = mk(tmp, http=403)
    r.load_state(); r.st.update(start=time.time(), markets=["KRW-AAA", "KRW-BBB"])
    t1 = c.datetime(2026, 11, 11, 5, 0, 0, tzinfo=c.timezone.utc).timestamp()
    r.on_message(json.dumps(TR).encode(), t1)
    r.close_all(); r.scan_closed(t1 + 7 * 3600)
    r.stop_up.set(); r.upload_loop()
    assert os.path.exists(os.path.join(tmp, "AAA_20261111T00.csv.gz")), os.listdir(tmp)      # 압축본은 남겨 둠
    assert r.st["upload_fail"] == 1 and r.st["last_upload_http"] == 403 and len(sent) == 1 and "업로드 실패" in sent[0]
    r.stop_up.clear(); r.scan_closed(t1 + 7 * 3600); r.stop_up.set(); r.upload_loop()        # 다시 시도, 경고는 한 번만
    assert r.st["upload_fail"] == 2 and len(sent) == 1


class FakeWS:
    """연결 1: 메시지 2개 뒤 끊김. 연결 2: 메시지 1개 뒤 조용함(타임아웃)."""
    n = 0
    def __init__(self):
        FakeWS.n += 1
        self.k, self.msgs, self.sent = FakeWS.n, [], []
        self.msgs = [json.dumps(TR).encode(), json.dumps(OB).encode()] if self.k == 1 else [json.dumps(dict(TR, sequential_id=9)).encode()]
    def connect(self): pass
    def send_text(self, s): self.sent.append(s)
    def ping(self): pass
    def close(self): pass
    def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        if self.k == 1:
            raise ConnectionError("drop")
        time.sleep(0.05)
        raise socket.timeout()


def test_end_to_end_short_run():
    tmp = tempfile.mkdtemp()
    FakeWS.n = 0
    orig_sleep = c.time.sleep
    c.time.sleep = lambda s: orig_sleep(min(s, 0.05))                 # 재연결 대기를 짧게
    try:
        r, sent, puts, ups = mk(tmp, days=1.5 / 86400, ws_factory=FakeWS)
        r.run()
    finally:
        c.time.sleep = orig_sleep
    assert "기록 시작" in sent[0] and "기록 끝" in sent[-1] and "체결 2건 · 호가 1건" in sent[-1], sent
    body = "".join(u[1] for u in ups)
    assert body.count("E,") >= 4 and "connect" in body                # 연결 2번 × 코인 2개에 연결 표시
    assert body.count("\nT,") + body.startswith("T,") >= 2 or body.count("T,") >= 2
    st = json.loads(puts["data/mmrec/status.json"])
    assert st["done"] and st["session"]["reconnects"] >= 1 and st["pending_files"] == 0 and st["uploaded_files"] >= 1, st
    assert not [f for f in os.listdir(tmp) if f.endswith(".csv") or f.endswith(".gz")]
    r2, sent2, _, _ = mk(tmp, ws_factory=FakeWS); r2.run(); assert not sent2          # 끝난 기록은 다시 시작하지 않음


def test_rank_rule():
    tmp = tempfile.mkdtemp()
    r = c.MMWS(base=tmp, days=0.5 / 86400, top_n=3, ws_factory=FakeWS, uploader=lambda p, n: 201, status_put=lambda *a: True, sender=lambda s: None,
               ranker=lambda n: [("KRW-USDT", 9e10), ("KRW-XRP", 5e10), ("KRW-BTC", 3e10)][:n])
    orig = c.time.sleep; c.time.sleep = lambda s: orig(min(s, 0.05))
    try:
        r.run()
    finally:
        c.time.sleep = orig
    assert r.st["markets"] == ["KRW-USDT", "KRW-XRP", "KRW-BTC"] and r.st["rank"][0] == ["KRW-USDT", 900.0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
