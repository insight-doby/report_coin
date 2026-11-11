"""서버에 올리기 전 검증: 새 웹소켓 기록기(server/coinbot.py 의 MMWS·MiniWS)를 실제 빗썸에 붙여
검증된 라이브러리(websocket-client)로 받은 것과 한 건씩 대조한다. 중간에 연결을 일부러 끊어 재연결도 본다.

통과 기준(미리 고정)
  1) 끊긴 구간 밖에서 체결 번호 집합이 완전히 같을 것 (빠짐 0, 남음 0)
  2) 끊긴 구간 밖에서 호가 메시지(코인·호가시각)의 99.9% 이상을 기록했을 것
  3) 일부러 끊은 뒤 10초 안에 다시 받기 시작할 것
  4) 파일의 모든 줄이 형식대로 읽힐 것
  5) 릴리스 첨부 업로드가 201 로 성공할 것
출력: data/research/mmws_validate.json (+ 기록기 원본 파일 사본)
"""
import glob, gzip, json, os, shutil, subprocess, sys, tempfile, threading, time
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websocket-client"], check=False)
sys.path.insert(0, "server")
import coinbot as c
import websocket

MIN = float(os.environ.get("VALIDATE_MINUTES", "8"))
MK = ["KRW-XRP", "KRW-XPL", "KRW-HYPE", "KRW-ETH", "KRW-USDT"]
OUT = "data/research/mmws_validate"
os.makedirs(OUT, exist_ok=True)
tmp = tempfile.mkdtemp()
ref_tr, ref_ob, ref_ev, up_codes, msgs = [], [], [], [], []
t_end = time.time() + MIN * 60 + 20


def ref_loop():
    sub = json.dumps([{"ticket": "ref"}, {"type": "trade", "codes": MK}, {"type": "orderbook", "codes": MK}, {"format": "DEFAULT"}])
    def on_message(ws, m):
        t = time.time()
        d = json.loads(m.decode() if isinstance(m, (bytes, bytearray)) else m)
        if d.get("type") == "trade":
            ref_tr.append((t, d["code"], d["sequential_id"], d["trade_timestamp"], d["timestamp"], d["stream_type"]))
        elif d.get("type") == "orderbook":
            ref_ob.append((t, d["code"], d["timestamp"], d["stream_type"]))
        if t > t_end:
            ws.close()
    while time.time() < t_end:
        app = websocket.WebSocketApp(c.MM_WS_URL, on_open=lambda ws: (ref_ev.append((time.time(), "open")), ws.send(sub)),
                                     on_message=on_message, on_error=lambda ws, e: ref_ev.append((time.time(), "error", str(e)[:100])),
                                     on_close=lambda ws, a, b: ref_ev.append((time.time(), "close")))
        app.run_forever(ping_interval=30, ping_timeout=10)
        time.sleep(1)


def uploader(path, name):
    shutil.copy(path, os.path.join(OUT, name))                       # 분석용 사본
    code = c.gh_release_upload(path, "validate-" + name)             # 실제 업로드 경로 검증 (이름 앞에 validate-)
    up_codes.append([name, code, os.path.getsize(path)])
    return code


rec = c.MMWS(base=tmp, days=MIN / 1440, markets=MK, uploader=uploader, status_put=lambda p, raw, m: msgs.append(["status", len(raw)]) or True,
             sender=lambda s: msgs.append(["tg", s[:80]]))
th_ref = threading.Thread(target=ref_loop, daemon=True); th_ref.start()
time.sleep(3)                                                         # 기준 쪽을 먼저 연다
t0 = time.time()
th_rec = threading.Thread(target=rec.run, daemon=True); th_rec.start()
time.sleep(MIN * 60 / 2)
t_drop = time.time()
try:
    rec.ws.sock.close()                                               # 일부러 끊기
except Exception as e:
    msgs.append(["drop_error", str(e)])
th_rec.join(timeout=MIN * 60 + 600)
time.sleep(3)
status = rec.status()

# ── 기록기 파일 읽기 ──
T, B, E, bad = {}, set(), [], 0
lines = 0
for fn in sorted(glob.glob(os.path.join(OUT, "*.csv.gz"))):
    code = "KRW-" + os.path.basename(fn).split("_")[0]
    for ln in gzip.open(fn, "rt"):
        lines += 1
        p = ln.rstrip("\n").split(",")
        try:
            if p[0] == "T" and len(p) == 9:
                T[(code, int(p[4]))] = (int(p[1]) / 1e6, int(p[3]), p[8]); float(p[6]); float(p[7])
            elif p[0] == "B" and len(p) == 6 + 4 * c.MM_LEVELS:
                B.add((code, int(p[2]))); [float(x) for x in p[4:]]
            elif p[0] == "E" and len(p) == 3:
                E.append((int(p[1]) / 1e6, p[2]))
            else:
                bad += 1
        except ValueError:
            bad += 1
connects = sorted({round(e[0], 3) for e in E if e[1] == "connect"})
t_back = min([x for x in connects if x > t_drop], default=None)
out_lo, out_hi = t_drop - 1.0, (t_back or t_drop) + 5.0                # 끊긴 구간(앞뒤 여유)
lo = max(t0, connects[0] if connects else t0) + 5
hi = min(max((v[0] for v in T.values()), default=t0), max((x[0] for x in ref_tr), default=t0)) - 5
inwin = lambda t: lo <= t <= hi and not (out_lo <= t <= out_hi)
R = {x[2]: x for x in ref_tr if x[5] == "REALTIME"}
ref_set = {(x[1], x[2]) for x in R.values() if inwin(x[4] / 1000) and inwin(x[0])}
rec_set = {k for k, v in T.items() if v[2] == "REALTIME"}
ref_all = {(x[1], x[2]) for x in ref_tr}
missing = sorted(ref_set - set(T))                                    # 기준에 있는데 기록기에 없음
extra = sorted(k for k, v in T.items() if v[2] == "REALTIME" and inwin(v[0]) and inwin(v[1] / 1000) and k not in ref_all)
ob_ref = {(x[1], x[2]) for x in ref_ob if x[3] == "REALTIME" and inwin(x[0]) and inwin(x[2] / 1e6)}
ob_missing = len(ob_ref - B)
res = {"minutes": MIN, "markets": MK, "host": os.uname().nodename, "git": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
       "window_s": round(hi - lo, 1), "forced_drop_at": round(t_drop - t0, 1), "reconnected_after_s": round(t_back - t_drop, 2) if t_back else None,
       "connect_events": len(connects), "ref_events": [e[1:] for e in ref_ev],
       "lines": lines, "bad_lines": bad, "trades_ref": len(ref_set), "trades_missing": len(missing), "trades_extra": len(extra),
       "book_ref": len(ob_ref), "book_missing": ob_missing, "book_coverage_pct": round(100 * (1 - ob_missing / max(1, len(ob_ref))), 4),
       "recorder_status": status, "uploads": up_codes, "messages": msgs}
res["pass"] = {"1_trades_identical": len(missing) == 0 and len(extra) == 0 and len(ref_set) > 50,
               "2_book_coverage": res["book_coverage_pct"] >= 99.9 and len(ob_ref) > 1000,
               "3_reconnect_10s": t_back is not None and (t_back - t_drop) <= 10,
               "4_lines_ok": bad == 0 and lines > 1000,
               "5_upload_201": bool(up_codes) and all(u[1] == 201 for u in up_codes)}
res["all_pass"] = all(res["pass"].values())
json.dump(res, open("data/research/mmws_validate.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in res.items() if k not in ("recorder_status",)}, ensure_ascii=False, indent=1))
print("기록기 지연(ms):", status["latency_ms"])
