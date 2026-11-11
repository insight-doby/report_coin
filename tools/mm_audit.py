"""데이터 품질 감사용 수집기 — 공개 시세만 읽음(키·주문 없음).

같은 시간 동안 세 가지 경로로 같은 시장을 동시에 받아 서로 대조할 수 있게 원자료를 남긴다.
  A. REST 체결  : 2초마다 최근 200건 (빠짐없이 받았다고 볼 수 있는 기준 자료)
  B. REST 호가  : 10초마다 (돌려주는 호가 칸 수, 거래소 시각, 왕복 시간)
  C. 웹소켓     : 체결 + 호가 실시간 (거래소 시각, 받은 시각, 순서, 중복, 칸 수)
분석은 tools/mm_audit_analyze.py 가 원자료 파일을 읽어서 한다(수집과 분석을 분리 → 다시 계산 가능).

출력: data/mm_audit/raw_<시작시각>.json.gz
"""
import gzip, json, os, platform, subprocess, sys, threading, time, traceback, uuid
from datetime import datetime, timezone, timedelta

subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websocket-client"], check=False)
import requests

REST = "https://api.bithumb.com/v1"
WS_URL = "wss://ws-api.bithumb.com/websocket/v1"
MARKETS = os.environ.get("MM_AUDIT_MARKETS", "KRW-XRP,KRW-XPL,KRW-HYPE,KRW-ETH,KRW-USDT").split(",")
MINUTES = float(os.environ.get("MM_AUDIT_MINUTES", "12"))
TR_EVERY, OB_EVERY, LEVELS = 2.0, 10.0, 5
KST = timezone(timedelta(hours=9))

out = {"meta": {"markets": MARKETS, "minutes": MINUTES, "host": platform.node(), "python": sys.version.split()[0],
                "rest": REST, "ws": WS_URL, "tr_every": TR_EVERY, "ob_every": OB_EVERY, "levels": LEVELS,
                "cols": {
                    "rest_trades": "first_poll,first_seen_local,market,sid,ts_ms,px,vol,side",
                    "rest_polls": "poll,market,t_send,t_recv,http,n,n_new,newest_sid,oldest_sid,order_ok",
                    "rest_ob": "t_send,t_recv,http,market,ts_ms,n_units,tot_ask,tot_bid,[b,bs,a,as]*levels",
                    "ws_trade": "recv,code,ts,trade_ts,sid,px,vol,side,stream",
                    "ws_ob": "recv,code,ts,stream,n_units,tot_ask,tot_bid,[b,bs,a,as]*levels"}},
       "rest_trades": [], "rest_polls": [], "rest_ob": [], "rest_dup_conflict": [], "rest_count_probe": {},
       "ws_trade": [], "ws_ob": [], "ws_events": [], "ws_other": [], "samples": {}, "errors": []}
lock = threading.Lock()
STOP = threading.Event()


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def note(kind, msg):
    with lock:
        out["errors"].append([time.time(), kind, str(msg)[:300]])


def rest_loop(t_end):
    s = requests.Session()
    seen = {}
    # count 상한 확인: 500을 요청하면 몇 건을 돌려주는가
    for m in MARKETS[:2]:
        try:
            r = s.get(REST + "/trades/ticks", params={"market": m, "count": 500}, timeout=10)
            out["rest_count_probe"][m] = {"http": r.status_code, "n": len(r.json()) if r.ok and isinstance(r.json(), list) else None}
        except Exception as e:
            note("count_probe", e)
    poll, next_ob = 0, 0.0
    while time.time() < t_end and not STOP.is_set():
        c0 = time.time()
        for m in MARKETS:
            t0 = time.time()
            try:
                r = s.get(REST + "/trades/ticks", params={"market": m, "count": 200}, timeout=10)
                t1 = time.time()
                data = r.json() if r.ok else None
            except Exception as e:
                note("rest_tr", e)
                out["rest_polls"].append([poll, m, t0, time.time(), -1, 0, 0, None, None, None])
                continue
            if not isinstance(data, list):
                out["rest_polls"].append([poll, m, t0, t1, r.status_code, 0, 0, None, None, None])
                continue
            if "rest_trade" not in out["samples"] and data:
                out["samples"]["rest_trade"] = data[0]
            sids = [x.get("sequential_id") for x in data]
            order_ok = all(a >= b for a, b in zip(sids, sids[1:])) if all(isinstance(z, (int, float)) for z in sids) else None
            n_new = 0
            for x in data:
                key = (m, x.get("sequential_id"))
                val = (x.get("timestamp"), f(x.get("trade_price")), f(x.get("trade_volume")), x.get("ask_bid"))
                if key in seen:
                    if seen[key] != val:
                        out["rest_dup_conflict"].append([m, x.get("sequential_id"), list(seen[key]), list(val)])
                    continue
                seen[key] = val
                n_new += 1
                out["rest_trades"].append([poll, round(t1, 3), m, x.get("sequential_id"), *val])
            out["rest_polls"].append([poll, m, round(t0, 3), round(t1, 3), r.status_code, len(data), n_new,
                                      sids[0] if sids else None, sids[-1] if sids else None, order_ok])
        if time.time() >= next_ob:
            next_ob = time.time() + OB_EVERY
            t0 = time.time()
            try:
                r = s.get(REST + "/orderbook", params={"markets": ",".join(MARKETS)}, timeout=10)
                t1 = time.time()
                data = r.json() if r.ok else []
                if "rest_orderbook" not in out["samples"] and data:
                    d0 = dict(data[0]); d0["orderbook_units"] = (d0.get("orderbook_units") or [])[:2]
                    out["samples"]["rest_orderbook"] = d0
                    out["samples"]["rest_orderbook_units_n"] = len(data[0].get("orderbook_units") or [])
                for o in data if isinstance(data, list) else []:
                    u = o.get("orderbook_units") or []
                    row = [round(t0, 3), round(t1, 3), r.status_code, o.get("market"), o.get("timestamp"), len(u),
                           f(o.get("total_ask_size")), f(o.get("total_bid_size"))]
                    for x in u[:LEVELS]:
                        row += [f(x.get("bid_price")), f(x.get("bid_size")), f(x.get("ask_price")), f(x.get("ask_size"))]
                    out["rest_ob"].append(row)
            except Exception as e:
                note("rest_ob", e)
        poll += 1
        time.sleep(max(0.0, TR_EVERY - (time.time() - c0)))


def ws_loop(t_end):
    try:
        import websocket
    except Exception as e:
        note("ws_import", e)
        return
    sub = json.dumps([{"ticket": "audit-" + uuid.uuid4().hex[:8]},
                      {"type": "trade", "codes": MARKETS}, {"type": "orderbook", "codes": MARKETS},
                      {"format": "DEFAULT"}])

    def on_open(ws):
        out["ws_events"].append([time.time(), "open"])
        ws.send(sub)

    def on_message(ws, msg):
        t = time.time()
        if STOP.is_set():
            return
        try:
            if isinstance(msg, (bytes, bytearray)):
                msg = msg.decode("utf-8")
            d = json.loads(msg)
        except Exception as e:
            note("ws_parse", e)
            return
        ty = d.get("type")
        if ty == "trade":
            if "ws_trade" not in out["samples"]:
                out["samples"]["ws_trade"] = d
            out["ws_trade"].append([round(t, 4), d.get("code"), d.get("timestamp"), d.get("trade_timestamp"), d.get("sequential_id"),
                                    f(d.get("trade_price")), f(d.get("trade_volume")), d.get("ask_bid"), d.get("stream_type")])
        elif ty == "orderbook":
            u = d.get("orderbook_units") or []
            if "ws_orderbook" not in out["samples"]:
                d0 = dict(d); d0["orderbook_units"] = u[:2]
                out["samples"]["ws_orderbook"] = d0
            row = [round(t, 4), d.get("code"), d.get("timestamp"), d.get("stream_type"), len(u),
                   f(d.get("total_ask_size")), f(d.get("total_bid_size"))]
            for x in u[:LEVELS]:
                row += [f(x.get("bid_price")), f(x.get("bid_size")), f(x.get("ask_price")), f(x.get("ask_size"))]
            out["ws_ob"].append(row)
        else:
            if len(out["ws_other"]) < 200:
                out["ws_other"].append([round(t, 3), d])
        if t >= t_end:
            ws.close()

    def on_error(ws, e):
        out["ws_events"].append([time.time(), "error", str(e)[:200]])

    def on_close(ws, code, reason):
        out["ws_events"].append([time.time(), "close", code, str(reason)[:200]])

    while time.time() < t_end and not STOP.is_set():
        try:
            app = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            note("ws_run", e)
        if time.time() < t_end:
            out["ws_events"].append([time.time(), "reconnect"])
            time.sleep(2)


def main():
    t0 = time.time()
    t_end = t0 + MINUTES * 60
    out["meta"]["start"] = t0
    out["meta"]["start_kst"] = datetime.fromtimestamp(t0, KST).isoformat(timespec="seconds")
    try:
        out["meta"]["git"] = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    ths = [threading.Thread(target=rest_loop, args=(t_end,), daemon=True), threading.Thread(target=ws_loop, args=(t_end,), daemon=True)]
    for th in ths:
        th.start()
    while time.time() < t_end + 3:
        time.sleep(1)
    STOP.set()
    for th in ths:
        th.join(timeout=15)
    out["meta"]["end"] = time.time()
    os.makedirs("data/mm_audit", exist_ok=True)
    name = f"data/mm_audit/raw_{datetime.fromtimestamp(t0, KST):%Y%m%d_%H%M}.json.gz"
    snap = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in out.items()}
    raw = json.dumps(snap, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    open(name, "wb").write(gzip.compress(raw))
    print(name, f"{len(raw) / 1e6:.1f}MB raw", "| rest_trades", len(out["rest_trades"]), "| rest_ob", len(out["rest_ob"]),
          "| ws_trade", len(out["ws_trade"]), "| ws_ob", len(out["ws_ob"]), "| ws_events", out["ws_events"][:6], "| errors", len(out["errors"]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        os.makedirs("data/mm_audit", exist_ok=True)
        open("data/mm_audit/error.txt", "w").write(traceback.format_exc())
        raise
