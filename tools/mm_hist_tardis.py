"""E2a — 과거 호가창+체결 기록으로 '지정가가 체결된 뒤 얼마가 남는가'(마크아웃) 측정. 공개 무료 표본만 사용.

자료: Tardis.dev 가 기록한 빗썸 웹소켓 자료 중 무료로 공개된 매달 1일치 (trades, book_snapshot_5).
종목: 결과를 보기 전에 규칙으로 고정 — 그 전날(UTC) 빗썸 일봉 거래대금 상위 N개 KRW 마켓.
가정: 수수료 0원. 내 주문의 줄 순서는 알 수 없으므로 양 끝 가정을 함께 계산한다.
  줄 앞 = 맨 앞 가격에서 난 모든 체결을 내가 받았다고 봄(낙관)
  줄 뒤 = 그 가격의 줄이 통째로 없어진 체결만 받았다고 봄(보수)
마크아웃 = 체결가 대비 τ초 뒤 중간값의 유리한 정도. 단위: 1호가, 그리고 bp(=0.01%).

사용: python tools/mm_hist_tardis.py 2026-10-01 2026-11-01
출력: data/research/tardis_<날짜>.json, data/research/tardis_summary.txt
"""
import gzip, io, json, math, os, sys, time, traceback
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests

TOP_N = int(os.environ.get("HIST_TOP_N", "10"))
TAUS = (1, 5, 30, 60, 300)
BLOCK_S = 600                     # 오차 계산용 묶음(10분)
WIPE_S = 0.3                      # 체결 0.3초 뒤 호가에서 그 가격 줄이 사라졌는지
T = "https://datasets.tardis.dev/v1/bithumb"
B = "https://api.bithumb.com/v1"
S = requests.Session(); S.headers["User-Agent"] = "report-coin-research"


def rank_prev_day(day: str):
    """day(UTC) 전날 일봉 거래대금 순위. 돌려주는 값: [(market, 거래대금), ...] 내림차순"""
    mk = [m["market"] for m in S.get(B + "/market/all", timeout=20).json() if m["market"].startswith("KRW-")]
    d = datetime.strptime(day, "%Y-%m-%d")
    prev = (d - timedelta(days=1)).strftime("%Y-%m-%d")
    to = d.strftime("%Y-%m-%dT09:00:00")                      # KST 09시 = UTC 0시. 그 직전 일봉이 전날(UTC) 봉
    rows = []
    for m in mk:
        for attempt in range(3):
            try:
                j = S.get(B + "/candles/days", params={"market": m, "count": 2, "to": to}, timeout=15).json()
                break
            except Exception:
                j = None; time.sleep(1 + attempt)
        if isinstance(j, list):
            for c in j:
                if str(c.get("candle_date_time_utc", ""))[:10] == prev:
                    rows.append((m, float(c.get("candle_acc_trade_price") or 0)))
        time.sleep(0.03)
    rows.sort(key=lambda x: -x[1])
    return rows, len(mk)


def fetch_csv(kind, day, sym, usecols=None):
    url = f"{T}/{kind}/{day.replace('-', '/')}/{sym}.csv.gz"
    r = S.get(url, timeout=120)
    if r.status_code != 200:
        return None, r.status_code, 0
    df = pd.read_csv(io.BytesIO(r.content), compression="gzip", usecols=usecols)
    return df, 200, len(r.content)


def analyze(tr: pd.DataFrame, ob: pd.DataFrame):
    """tr: timestamp(µs), side(buy/sell = 치고 들어온 쪽), price, amount / ob: timestamp(µs), asks[i].price, bids[i].price, asks[0].amount, bids[0].amount"""
    ob = ob.sort_values("timestamp", kind="stable").reset_index(drop=True)
    tr = tr.sort_values("timestamp", kind="stable").reset_index(drop=True)
    ots = ob["timestamp"].to_numpy(np.int64)
    b1 = ob["bids[0].price"].to_numpy(float); a1 = ob["asks[0].price"].to_numpy(float)
    bq = ob["bids[0].amount"].to_numpy(float); aq = ob["asks[0].amount"].to_numpy(float)
    bids = np.stack([ob[f"bids[{i}].price"].to_numpy(float) for i in range(5)], 1)
    asks = np.stack([ob[f"asks[{i}].price"].to_numpy(float) for i in range(5)], 1)
    mid = (a1 + b1) / 2
    ok_book = np.isfinite(a1) & np.isfinite(b1) & (a1 > b1)
    px_all = np.unique(np.concatenate([bids[::50].ravel(), asks[::50].ravel()])); px_all = px_all[np.isfinite(px_all)]
    diffs = np.diff(px_all); diffs = diffs[diffs > 1e-12]
    tick = float(np.round(diffs.min(), 10)) if diffs.size else float("nan")
    med_mid = float(np.nanmedian(mid))
    sp_ticks = np.round((a1 - b1) / tick)
    gaps = np.diff(ots) / 1e6
    res = {"book_rows": int(len(ob)), "trades": int(len(tr)), "tick": tick, "tick_bp": round(tick / med_mid * 1e4, 3),
           "price_open": float(mid[0]), "price_close": float(mid[-1]), "day_return_pct": round((mid[-1] / mid[0] - 1) * 100, 2),
           "day_range_pct": round((np.nanmax(mid) / np.nanmin(mid) - 1) * 100, 2),
           "spread_ticks_med": float(np.nanmedian(sp_ticks)), "one_tick_share": round(float(np.mean(sp_ticks <= 1)), 4),
           "crossed_or_bad_book_share": round(float(1 - ok_book.mean()), 5),
           "q_bid_krw_med": float(np.nanmedian(b1 * bq)), "q_ask_krw_med": float(np.nanmedian(a1 * aq)),
           "book_gap_s": {"p50": round(float(np.median(gaps)), 3), "p99": round(float(np.percentile(gaps, 99)), 2), "max": round(float(gaps.max()), 1)},
           "book_ts_inversions_in_file": int((np.diff(ob["timestamp"].to_numpy(np.int64)) < 0).sum())}
    tts = tr["timestamp"].to_numpy(np.int64)
    px = tr["price"].to_numpy(float); amt = tr["amount"].to_numpy(float)
    sell = (tr["side"].to_numpy() == "sell")                     # 매도 주문이 침 → 매수 호가의 지정가가 체결
    val = px * amt
    res["trade_value_krw"] = float(val.sum())
    i0 = np.searchsorted(ots, tts, "left") - 1                   # 체결 시각보다 앞선 마지막 호가
    have = (i0 >= 0)
    i0c = np.clip(i0, 0, len(ots) - 1)
    best = np.where(sell, b1[i0c], a1[i0c])
    at_best = have & (px == best) & ok_book[i0c]
    own = np.where(sell[:, None], bids[i0c], asks[i0c])
    in_top5 = have & (own == px[:, None]).any(1)
    inside = have & (px > b1[i0c]) & (px < a1[i0c])
    n_have = int(have.sum())
    res["align"] = {"n": n_have, "at_best_pct": round(100 * at_best.sum() / n_have, 2), "top5_same_side_pct": round(100 * in_top5.sum() / n_have, 2),
                    "inside_spread_pct": round(100 * inside.sum() / n_have, 2),
                    "opposite_meaning_at_best_pct": round(100 * (have & (px == np.where(sell, a1[i0c], b1[i0c]))).sum() / n_have, 2)}
    last_ok = tts + int(TAUS[-1] * 1e6) <= ots[-1]
    use = at_best & last_ok
    sgn = np.where(sell, 1.0, -1.0)
    edge = {"0-": sgn * (mid[i0c] - px)}
    for tau in TAUS:
        j = np.clip(np.searchsorted(ots, tts + int(tau * 1e6), "right") - 1, 0, len(ots) - 1)
        edge[f"{tau}s"] = sgn * (mid[j] - px)
    ja = np.clip(np.searchsorted(ots, tts + int(WIPE_S * 1e6), "right") - 1, 0, len(ots) - 1)   # 체결 0.3초 뒤 '그 시점'의 호가
    wiped = np.where(sell, b1[ja] < px, a1[ja] > px)
    blk = (tts // int(BLOCK_S * 1e6))
    seg = ((tts - tts.min()) // int(6 * 3600 * 1e6)).clip(0, 3)   # 6시간 구간 4개
    out = {}
    for name, sel in (("front", use), ("back", use & wiped)):
        n = int(sel.sum())
        o = {"n": n, "value_krw": float(val[sel].sum())}
        if n >= 30:
            for lab, e in edge.items():
                et = e[sel] / tick; v = val[sel]; bp = e[sel] / px[sel] * 1e4
                bm = pd.Series(et).groupby(blk[sel]).mean().to_numpy()
                bv = pd.DataFrame({"x": bp * v, "v": v}).groupby(blk[sel]).sum()
                bvw = (bv["x"] / bv["v"]).to_numpy()
                o[lab] = {"mean_ticks": round(float(et.mean()), 4), "se_ticks": round(float(bm.std(ddof=1) / math.sqrt(len(bm))), 4) if len(bm) > 2 else None,
                          "vw_ticks": round(float((et * v).sum() / v.sum()), 4), "mean_bp": round(float(bp.mean()), 4), "vw_bp": round(float((bp * v).sum() / v.sum()), 4),
                          "vw_bp_block_se": round(float(np.nanstd(bvw, ddof=1) / math.sqrt(len(bvw))), 4) if len(bvw) > 2 else None, "blocks": int(len(bm))}
            e60 = edge["60s"]
            o["vw_bp_60s_by_6h"] = [round(float((e60[sel & (seg == k)] / px[sel & (seg == k)] * 1e4 * val[sel & (seg == k)]).sum() / val[sel & (seg == k)].sum()), 3)
                                    if (sel & (seg == k)).sum() >= 20 else None for k in range(4)]
        out[name] = o
    res["markout"] = out
    res["share_wiped"] = round(float((use & wiped).sum() / max(1, use.sum())), 4)
    res["excluded_not_at_best_pct"] = round(100 * (1 - at_best.sum() / n_have), 2)
    return res


def run_day(day, syms=None):
    os.makedirs("data/research", exist_ok=True)
    R = {"day": day, "rule": f"전날(UTC) 빗썸 일봉 거래대금 상위 {TOP_N}개 KRW 마켓", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "params": {"taus": TAUS, "block_s": BLOCK_S, "wipe_s": WIPE_S, "fee": 0}, "symbols": {}}
    try:
        R["git"] = os.popen("git rev-parse HEAD").read().strip()
    except Exception:
        pass
    if syms is None:
        rank, n_mk = rank_prev_day(day)
        R["rank_prev_day"] = [[m, round(v / 1e8, 1)] for m, v in rank[:TOP_N + 10]]
        R["markets_checked"] = n_mk
        syms = [m for m, _ in rank[:TOP_N]]
    R["selected"] = syms
    obcols = ["timestamp"] + [f"{s}[{i}].price" for i in range(5) for s in ("asks", "bids")] + ["asks[0].amount", "bids[0].amount"]
    for sym in syms:
        try:
            tr, c1, n1 = fetch_csv("trades", day, sym, ["timestamp", "side", "price", "amount"])
            ob, c2, n2 = fetch_csv("book_snapshot_5", day, sym, obcols)
            if tr is None or ob is None or len(tr) < 50 or len(ob) < 50:
                R["symbols"][sym] = {"error": f"자료 없음 또는 부족 (trades http {c1}, book http {c2})"}
                continue
            r = analyze(tr, ob)
            r["bytes"] = {"trades": n1, "book": n2}
            R["symbols"][sym] = r
            print(day, sym, "trades", r["trades"], "book", r["book_rows"], "at_best", r["align"]["at_best_pct"])
        except Exception:
            R["symbols"][sym] = {"error": traceback.format_exc()[-600:]}
            print(day, sym, "ERROR")
    json.dump(R, open(f"data/research/tardis_{day}.json", "w"), ensure_ascii=False, indent=1)
    return R


def table(Rs):
    L = []
    for R in Rs:
        L.append(f"== {R['day']} | {R['rule']} | 선정: {', '.join(s[4:] for s in R['selected'])}")
        L.append(f"{'코인':7s}{'등락%':>7s}{'변동폭%':>7s}{'1호가bp':>8s}{'호가차':>5s}{'체결수':>8s}{'맨앞일치%':>8s}{'소진비율':>7s} | 줄 앞: 0- / 1s / 60s(금액가중 bp ±) / 300s | 줄 뒤: 1s / 60s(금액가중 bp) | 6시간별 60s")
        for sym, r in R["symbols"].items():
            if "error" in r:
                L.append(f"{sym[4:]:7s} {r['error'][:80]}"); continue
            f, b = r["markout"]["front"], r["markout"]["back"]
            g = lambda o, k, key="vw_bp": (f"{o[k][key]:+.2f}" if k in o else "  - ")
            L.append(f"{sym[4:]:7s}{r['day_return_pct']:>+7.1f}{r['day_range_pct']:>7.1f}{r['tick_bp']:>8.2f}{r['spread_ticks_med']:>5.0f}{r['trades']:>8d}{r['align']['at_best_pct']:>8.1f}{r['share_wiped']:>7.2f}"
                     f" | {g(f, '0-')} / {g(f, '1s')} / {g(f, '60s')} ±{f.get('60s', {}).get('vw_bp_block_se')} / {g(f, '300s')}"
                     f" | {g(b, '1s')} / {g(b, '60s')} (n={b['n']}) | {f.get('vw_bp_60s_by_6h')}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    days = [a for a in sys.argv[1:] if a[:2] == "20"] or ["2026-10-01", "2026-11-01"]
    Rs = [run_day(d) for d in days]
    txt = table(Rs)
    open("data/research/tardis_summary.txt", "w").write(txt)
    print(txt)
