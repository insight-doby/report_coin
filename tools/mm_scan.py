"""지정가 양쪽 매매(마켓 메이킹) 후보 코인 조사 — 주문 없음, 공개 시세만 사용.

거래대금 상위 코인의 호가창과 체결을 일정 시간 기록해서 코인별로 잰다:
  1호가 비율(%) · 실제 호가 차이(몇 호가) · 맨 앞 줄 대기 금액 · 분당 체결 금액(방향별)
  · 맨 앞 줄이 다 체결되는 데 걸리는 시간 · 5분 가격 흔들림
→ data/mm_scan.json, data/mm_scan.txt
"""
import os, sys, json, time, statistics as st, traceback
from datetime import datetime, timezone, timedelta
import requests

API = "https://api.bithumb.com/v1"
MINUTES = float(os.environ.get("MM_MINUTES", "18"))
TOP_N = int(os.environ.get("MM_TOP", "45"))
MIN_VOL_EOK = 10            # 24시간 거래대금 10억 미만은 제외
KST = timezone(timedelta(hours=9))
S = requests.Session()


def get(path, **params):
    for i in range(3):
        try:
            r = S.get(API + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + i); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(0.5 + i)
    return None


def tick_of(units):
    ps = sorted({float(u["ask_price"]) for u in units} | {float(u["bid_price"]) for u in units})
    diffs = [round(b - a, 10) for a, b in zip(ps, ps[1:]) if b > a]
    return min(diffs) if diffs else None


def main():
    mk = [m["market"] for m in get("/market/all", isDetails="false") or [] if m["market"].startswith("KRW-")]
    tick = []
    for i in range(0, len(mk), 50):
        tick += get("/ticker", markets=",".join(mk[i:i + 50])) or []
    tick = [t for t in tick if float(t.get("acc_trade_price_24h") or 0) >= MIN_VOL_EOK * 1e8]
    tick.sort(key=lambda t: -float(t["acc_trade_price_24h"]))
    cand = [t["market"] for t in tick[:TOP_N]]
    vol24 = {t["market"]: float(t["acc_trade_price_24h"]) for t in tick}
    print(f"후보 {len(cand)}개 / 기록 {MINUTES}분")

    ob = {m: [] for m in cand}          # (ts, bid, ask, bid_krw, ask_krw, tick)
    trades = {m: {} for m in cand}      # sequential_id -> (ts, price, krw, side)
    t0 = time.time(); last_tr = 0
    while time.time() - t0 < MINUTES * 60:
        now = time.time()
        for i in range(0, len(cand), 10):
            for o in get("/orderbook", markets=",".join(cand[i:i + 10])) or []:
                u = o.get("orderbook_units") or []
                if not u:
                    continue
                b, a = float(u[0]["bid_price"]), float(u[0]["ask_price"])
                ob[o["market"]].append((now, b, a, b * float(u[0]["bid_size"]), a * float(u[0]["ask_size"]), tick_of(u)))
        if now - last_tr > 50:
            for m in cand:
                for x in get("/trades/ticks", market=m, count=200) or []:
                    sid = x.get("sequential_id") or (x.get("timestamp"), x.get("trade_price"), x.get("trade_volume"))
                    p = float(x["trade_price"])
                    trades[m][str(sid)] = (x["timestamp"] / 1000, p, p * float(x["trade_volume"]), x.get("ask_bid"))
                time.sleep(0.05)
            last_tr = now
        time.sleep(max(0, 15 - (time.time() - now)))

    rows = []
    for m in cand:
        s = ob[m]
        if len(s) < 10:
            continue
        tk = st.median([x[5] for x in s if x[5]] or [0])
        if not tk:
            continue
        mid = [(x[1] + x[2]) / 2 for x in s]
        sp_ticks = [round((x[2] - x[1]) / tk) for x in s]
        t_start, t_end = s[0][0], s[-1][0]
        tr = [v for v in trades[m].values() if t_start - 60 <= v[0] <= t_end]
        span_min = max(1.0, (t_end - t_start) / 60)
        if tr:
            span_min = max(1.0, (max(v[0] for v in tr) - min(v[0] for v in tr)) / 60)
        sell_krw = sum(v[2] for v in tr if v[3] == "ASK") / span_min   # 매도 주문이 매수 호가를 친 금액 → 내 매수가 체결되는 쪽
        buy_krw = sum(v[2] for v in tr if v[3] == "BID") / span_min    # 내 매도가 체결되는 쪽
        qb = st.median([x[3] for x in s]); qa = st.median([x[4] for x in s])
        # 5분(=20샘플) 흔들림: 가운데값 변화의 표준편차(%)
        step = max(1, int(300 / 15))
        r5 = [(mid[i] / mid[i - step] - 1) * 100 for i in range(step, len(mid))]
        vol5 = st.pstdev(r5) if len(r5) > 2 else None
        px = mid[-1]
        rows.append({
            "market": m, "price": px, "vol24_eok": vol24.get(m, 0) / 1e8,
            "tick": tk, "tick_pct": tk / px * 100,
            "spread_ticks_med": st.median(sp_ticks), "one_tick_share": sum(1 for z in sp_ticks if z <= 1) / len(sp_ticks),
            "spread_pct_med": st.median([(x[2] - x[1]) / ((x[1] + x[2]) / 2) * 100 for x in s]),
            "queue_bid_krw": qb, "queue_ask_krw": qa,
            "sell_krw_min": sell_krw, "buy_krw_min": buy_krw,
            "bid_clear_min": qb / sell_krw if sell_krw else None,
            "ask_clear_min": qa / buy_krw if buy_krw else None,
            "trades_min": len(tr) / span_min, "vol5_pct": vol5,
            "move_window_pct": (mid[-1] / mid[0] - 1) * 100, "samples": len(s),
        })
    for r in rows:   # 점수: 한 번 왕복에 버는 몫(호가 차이) ÷ 5분 흔들림, 줄이 너무 길면 감점
        earn = r["spread_pct_med"]
        risk = r["vol5_pct"] or 1e9
        clear = max(r["bid_clear_min"] or 999, r["ask_clear_min"] or 999)
        r["score"] = earn / risk * (1 if clear <= 5 else 0.5 if clear <= 15 else 0.1)
    rows.sort(key=lambda r: -r["score"])
    at = datetime.now(KST).isoformat(timespec="seconds")
    json.dump({"at": at, "minutes": MINUTES, "rows": rows}, open("data/mm_scan.json", "w"), ensure_ascii=False, indent=1)
    L = [f"마켓 메이킹 후보 조사 {at} · {MINUTES:.0f}분 기록 · {len(rows)}개",
         f"{'코인':10s}{'가격':>12s}{'1호가%':>8s}{'호가차(호가)':>10s}{'1호가비율':>8s}{'호가차%':>8s}{'앞줄(매수/매도)만':>18s}{'분당체결(매도/매수)만':>20s}{'줄소진분(매수/매도)':>18s}{'5분흔들%':>9s}{'구간%':>7s}{'24h억':>7s}{'점수':>6s}"]
    for r in rows:
        f = lambda v: f"{v:.1f}" if v is not None else "-"
        L.append(f"{r['market'][4:]:10s}{r['price']:>12,.6g}{r['tick_pct']:>8.3f}{r['spread_ticks_med']:>10.0f}{r['one_tick_share']*100:>7.0f}%{r['spread_pct_med']:>8.3f}"
                 f"{r['queue_bid_krw']/1e4:>9,.0f}/{r['queue_ask_krw']/1e4:<8,.0f}{r['sell_krw_min']/1e4:>10,.0f}/{r['buy_krw_min']/1e4:<9,.0f}"
                 f"{f(r['bid_clear_min']):>9s}/{f(r['ask_clear_min']):<8s}{f(r['vol5_pct']):>9s}{r['move_window_pct']:>+7.2f}{r['vol24_eok']:>7.0f}{r['score']:>6.2f}")
    open("data/mm_scan.txt", "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        open("data/mm_scan.txt", "w").write(traceback.format_exc()); raise
