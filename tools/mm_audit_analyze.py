"""감사 원자료(data/mm_audit/raw_*.json.gz) 분석 — 수집과 분리되어 있어 언제든 다시 계산 가능.

사용: python tools/mm_audit_analyze.py data/mm_audit/raw_YYYYMMDD_HHMM.json.gz
출력: 화면 보고 + 같은 폴더 summary_*.json

용어
  체결 시각   = 웹소켓 trade_timestamp (REST timestamp 와 같은 값, 밀리초)
  발송 시각   = 웹소켓 체결 메시지의 timestamp (밀리초)
  호가 시각   = 웹소켓 호가 메시지의 timestamp (마이크로초)
  ASK 체결    = 매도 주문이 들어와 '매수 호가'에 걸려 있던 지정가를 체결시킴 (지정가 매수자가 산 것)
  BID 체결    = 매수 주문이 들어와 '매도 호가'에 걸려 있던 지정가를 체결시킴 (지정가 매도자가 판 것)

보는 것
  A. 건수·필드            B. REST 체결 번호(sequential_id)의 성질과 그로 인한 누락
  C. 웹소켓 체결 vs REST  D. 지연 분해            E. 웹소켓 호가(칸 수·빈도·순서) / REST 호가의 낡은 정도
  F. 호가-체결 정합       G. 폴링 간격별 누락 재현  H. 체결 후 가격 변화(마크아웃, 탐색용)
"""
import bisect, gzip, json, math, os, sys
from collections import Counter, defaultdict
import numpy as np


def pct(a, qs=(50, 90, 99), scale=1.0, nd=4):
    a = np.asarray(a, float) * scale
    if not a.size:
        return {"n": 0}
    return {f"p{q}": round(float(np.percentile(a, q)), nd) for q in qs} | {"min": round(float(a.min()), nd), "max": round(float(a.max()), nd), "n": int(a.size)}


def main(path):
    d = json.load(gzip.open(path))
    M = d["meta"]["markets"]
    fmt = d["meta"].get("fmt", 1)
    R = {"file": os.path.basename(path), "meta": {k: d["meta"].get(k) for k in ("start_kst", "minutes", "host", "git", "markets", "fmt")}}
    P = print
    P(f"=== 감사 분석: {R['file']} | 시작 {d['meta'].get('start_kst')} | {d['meta']['minutes']}분 | 수집 서버 {d['meta'].get('host')} | 형식 v{fmt} ===")

    # ── 자료 정리 ──
    # REST: 수집기 v1 은 번호(sequential_id)로 중복을 걸러 일부를 버렸고, 버린 것은 rest_dup_conflict 에 내용이 남아 있다 → 합쳐서 복원
    rest_kept = defaultdict(list)       # 번호 기준으로 남긴 것 (ts_ms, px, vol, side)
    rest_full = defaultdict(set)
    first_by_sid = set()
    for fp, seen, m, sid, ts, px, vol, side in d["rest_trades"]:
        rest_full[m].add((ts, px, vol, side))
        if fmt >= 2:                                  # v2 는 전부 저장 → '번호로 걸렀다면' 을 여기서 재현
            if (m, sid) in first_by_sid:
                continue
            first_by_sid.add((m, sid))
        rest_kept[m].append((ts, px, vol, side, sid))
    for m, sid, a, b in d.get("rest_dup_conflict", []):
        rest_full[m].add(tuple(a)); rest_full[m].add(tuple(b))
    ws_all = defaultdict(list)          # (recv_s, msg_ts_ms, trade_ts_ms, sid, px, vol, side, stream)
    for recv, c, ts, tts, sid, px, vol, side, stream in d["ws_trade"]:
        ws_all[c].append((recv, ts, tts, sid, px, vol, side, stream))
    wt = {m: [x for x in ws_all[m] if x[7] != "SNAPSHOT"] for m in M}
    wo = defaultdict(list)              # (recv_s, ts_s, stream, n_units, [b,bs,a,as]*L)
    for row in d["ws_ob"]:
        wo[row[1]].append((row[0], row[2] / 1e6, row[3], row[4], row[7:]))
    opens = [e[0] for e in d["ws_events"] if e[1] == "open"]

    P("\n[A] 건수")
    R["A"] = {"ws_events": [e[1:] for e in d["ws_events"]], "errors": dict(Counter(e[1] for e in d["errors"])),
              "rest_count_probe": d.get("rest_count_probe"), "rest_ob_units": d["samples"].get("rest_orderbook_units_n"),
              "fields": {k: sorted(v.keys()) for k, v in d["samples"].items() if isinstance(v, dict)}}
    for m in M:
        R["A"][m] = {"rest_rows_kept": len(rest_kept[m]), "rest_full": len(rest_full[m]), "ws_trades": len(ws_all[m]), "ws_ob": len(wo[m])}
        P(f"  {m[4:]:5s} REST 체결(번호로 거른 뒤) {len(rest_kept[m]):5d} / 복원 {len(rest_full[m]):5d} | 웹소켓 체결 {len(ws_all[m]):5d} | 웹소켓 호가 {len(wo[m]):6d}")
    P(f"  웹소켓 연결: {R['A']['ws_events']} | 수집 오류 {R['A']['errors']} | REST count=500 → {d.get('rest_count_probe')}")

    P("\n[B] REST 체결 번호(sequential_id)는 체결마다 고유한가")
    n_eq = sum(1 for m in M for x in rest_kept[m] if x[4] == x[0] * 10000)
    n_all = sum(len(rest_kept[m]) for m in M)
    R["B"] = {"sid_equals_ts_x10000": [n_eq, n_all]}
    P(f"  번호 = 체결 시각(ms)×10000 인 건: {n_eq}/{n_all} → 같은 1/1000초에 난 체결은 번호가 같음 (고유 번호 아님)")
    ws_sid_dup = sum(len(wt[m]) - len({x[3] for x in wt[m]}) for m in M)
    P(f"  웹소켓 번호 중복: {ws_sid_dup}건 (웹소켓 번호는 REST 번호와 다른 체계)")
    R["B"]["ws_sid_dup"] = ws_sid_dup

    P("\n[C] 웹소켓 체결 vs REST(복원) — 내용(체결 시각·가격·수량·방향)으로 한 건씩 대조")
    R["C"] = {}
    tot_w = tot_missing_ws = tot_missing_rest = 0
    for m in M:
        w = wt[m]
        if not w or not opens:
            R["C"][m] = {"n": 0}; continue
        lo = (opens[0] + 5) * 1000; hi = (max(x[0] for x in w) - 5) * 1000
        wc = Counter((x[2], x[4], x[5], x[6]) for x in w if lo <= x[2] <= hi)
        W = set(wc); F = {k for k in rest_full[m] if lo <= k[0] <= hi}
        K = {(x[0], x[1], x[2], x[3]) for x in rest_kept[m] if lo <= x[0] <= hi}
        val = lambda S: sum(k[1] * k[2] for k in S)
        inv_sid = sum(1 for a, b in zip(w, w[1:]) if b[3] < a[3]); inv_ts = sum(1 for a, b in zip(w, w[1:]) if b[2] < a[2])
        byms = Counter(k[0] for k in W)
        R["C"][m] = {"window_s": round((hi - lo) / 1000, 1), "ws": len(W), "rest_full": len(F), "missing_in_ws": len(F - W), "missing_in_rest": len(W - F),
                     "ws_same_content_pairs": sum(v - 1 for v in wc.values() if v > 1), "order_inversions_sid": inv_sid, "order_inversions_ts": inv_ts,
                     "rest_kept_by_sid": len(K), "lost_by_sid_dedupe_count_pct": round(100 * (1 - len(K) / len(W)), 2) if W else None,
                     "lost_by_sid_dedupe_value_pct": round(100 * (1 - val(K) / val(W)), 2) if W else None,
                     "same_ms_multi_fill_pct": round(100 * sum(v for v in byms.values() if v > 1) / len(W), 1) if W else None, "same_ms_max": max(byms.values()) if byms else 0}
        tot_w += len(F); tot_missing_ws += len(F - W); tot_missing_rest += len(W - F)
        c = R["C"][m]
        P(f"  {m[4:]:5s} {c['window_s']:.0f}초: 웹소켓 {c['ws']} / REST 복원 {c['rest_full']} | 웹소켓에 없음 {c['missing_in_ws']} · REST에 없음 {c['missing_in_rest']} | 순서 역전 {inv_sid}/{inv_ts}"
          f" | 번호로 걸렀을 때 누락: 건수 {c['lost_by_sid_dedupe_count_pct']}% 금액 {c['lost_by_sid_dedupe_value_pct']}% | 같은 ms 다건 {c['same_ms_multi_fill_pct']}% (최대 {c['same_ms_max']}건)")
    R["C"]["total"] = {"ref": tot_w, "missing_in_ws": tot_missing_ws, "missing_in_rest": tot_missing_rest,
                       "coverage_pct": round(100 * (1 - tot_missing_ws / tot_w), 3) if tot_w else None}
    P(f"  합계: 기준 {tot_w}건 중 웹소켓에 없음 {tot_missing_ws}건 → 포함률 {R['C']['total']['coverage_pct']}%")

    P("\n[D] 지연 분해 (밀리초). '발송→수신'에는 수집 서버와 거래소의 시계 차이가 섞여 있음")
    R["D"] = {}
    for m in M:
        w = wt[m]
        if not w:
            continue
        pub = [(x[1] - x[2]) for x in w]; net = [x[0] * 1000 - x[1] for x in w]; tot = [x[0] * 1000 - x[2] for x in w]
        ob_lat = [(x[0] - x[1]) * 1000 for x in wo[m]]
        R["D"][m] = {"trade_to_publish_ms": pct(pub, nd=0), "publish_to_recv_ms": pct(net, nd=0), "trade_to_recv_ms": pct(tot, nd=0), "book_ts_to_recv_ms": pct(ob_lat, nd=0)}
        P(f"  {m[4:]:5s} 체결→발송 {R['D'][m]['trade_to_publish_ms']} | 발송→수신 p50 {R['D'][m]['publish_to_recv_ms']['p50']:.0f} | 체결→수신 p50 {R['D'][m]['trade_to_recv_ms']['p50']:.0f} p99 {R['D'][m]['trade_to_recv_ms']['p99']:.0f}"
          f" | 호가 시각→수신 p50 {R['D'][m]['book_ts_to_recv_ms']['p50']:.0f} p99 {R['D'][m]['book_ts_to_recv_ms']['p99']:.0f}")
    polls = [x for x in d["rest_polls"] if x[4] == 200]
    R["D"]["rest_rtt_ms"] = pct([(x[3] - x[2]) * 1000 for x in polls], nd=0)
    P(f"  REST 왕복 시간: {R['D']['rest_rtt_ms']} (실패 {sum(1 for x in d['rest_polls'] if x[4] != 200)}회)")

    P("\n[E] 웹소켓 호가")
    R["E"] = {}
    ticks = {}
    for m in M:
        o = wo[m]
        if not o:
            R["E"][m] = {"n": 0}; continue
        dur = o[-1][1] - o[0][1]
        gaps = [(b[1] - a[1]) * 1000 for a, b in zip(o, o[1:])]
        prices = sorted({v for x in o[:: max(1, len(o) // 400)] for v in (x[4][0::4] + x[4][2::4]) if v})
        diffs = [round(b - a, 10) for a, b in zip(prices, prices[1:]) if b > a]
        tick = min(diffs) if diffs else None
        ticks[m] = tick
        sp = [round((x[4][2] - x[4][0]) / tick) for x in o]
        mid = float(np.median([(x[4][0] + x[4][2]) / 2 for x in o]))
        R["E"][m] = {"n": len(o), "streams": dict(Counter(x[2] for x in o)), "units": dict(Counter(x[3] for x in o)), "per_sec": round(len(o) / dur, 2),
                     "ts_inversions": sum(1 for g in gaps if g < 0), "same_ts": sum(1 for g in gaps if g == 0), "crossed": sum(1 for x in o if x[4][0] >= x[4][2]),
                     "gap_ms": pct(gaps, qs=(10, 50, 90, 99), nd=1), "tick": tick, "tick_pct": round(tick / mid * 100, 4),
                     "spread_ticks_med": float(np.median(sp)), "one_tick_share": round(sum(1 for s in sp if s <= 1) / len(sp), 4),
                     "q_bid_krw_med": round(float(np.median([x[4][0] * x[4][1] for x in o]))), "q_ask_krw_med": round(float(np.median([x[4][2] * x[4][3] for x in o])))}
        e = R["E"][m]
        P(f"  {m[4:]:5s} {e['n']}건(초당 {e['per_sec']}) 칸 수 {e['units']} | 시각 역전 {e['ts_inversions']} 꼬임 {e['crossed']} | 갱신 간격(ms) {e['gap_ms']}"
          f" | 1호가 {e['tick_pct']}% · 호가차 중앙 {e['spread_ticks_med']:.0f}칸(1칸 비율 {e['one_tick_share'] * 100:.0f}%) | 맨 앞 대기 {e['q_bid_krw_med'] / 1e4:,.0f}만/{e['q_ask_krw_med'] / 1e4:,.0f}만")
    ro = [r for r in d["rest_ob"]]
    age = [(r[1] - r[4] / 1000) for r in ro if r[4]]
    R["E"]["rest_ob"] = {"n": len(ro), "units": dict(Counter(r[5] for r in ro)), "age_at_receipt_s": pct(age, nd=2)}
    P(f"  REST 호가: 칸 수 {R['E']['rest_ob']['units']} | 받았을 때 이미 지난 시간(초) {R['E']['rest_ob']['age_at_receipt_s']}")

    P("\n[F] 체결 가격 = 체결 직전 호가의 맨 앞 가격인가 (호가 시각 < 체결 시각 인 마지막 호가)")
    R["F"] = {}
    for m in M:
        o, w = wo[m], wt[m]
        if not o or not w:
            continue
        ts = [x[1] for x in o]
        c = Counter()
        for x in w:
            i = bisect.bisect_left(ts, x[2] / 1000) - 1
            if i < 0:
                continue
            lv = o[i][4]; bids, asks = lv[0::4], lv[2::4]
            px = x[4]; own, opp, own_px = (bids, asks, bids[0]) if x[6] == "ASK" else (asks, bids, asks[0])
            if px == own_px: c["best"] += 1
            elif px in own: c["deeper"] += 1
            elif bids[0] < px < asks[0]: c["inside"] += 1
            elif px == opp[0]: c["opposite_best"] += 1
            else: c["other"] += 1
        n = sum(c.values())
        scan = {}
        for delta in (-0.5, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.5):
            hit = k = 0
            for x in w:
                i = bisect.bisect_left(ts, x[2] / 1000 + delta) - 1
                if i < 0:
                    continue
                k += 1
                hit += (x[4] == o[i][4][0]) if x[6] == "ASK" else (x[4] == o[i][4][2])
            scan[delta] = round(100 * hit / k, 1) if k else None
        R["F"][m] = {"n": n} | {k: round(100 * v / n, 1) for k, v in c.items()} | {"shift_scan_pct": scan}
        P(f"  {m[4:]:5s} n={n} 맨 앞 {100 * c['best'] / n:.1f}% | 같은 쪽 2~5칸 {100 * c['deeper'] / n:.1f}% | 호가 사이 {100 * c['inside'] / n:.1f}% | 반대쪽 맨 앞 {100 * c['opposite_best'] / n:.1f}% | 기타 {100 * c['other'] / n:.1f}%"
          f" || 호가를 δ초 옮겼을 때: " + " ".join(f"{k:+.2f}:{v:.0f}" for k, v in scan.items()))

    P("\n[G] 'T초마다 최근 200건' 방식이었다면 놓쳤을 건수 (웹소켓 체결을 기준으로 재현)")
    R["G"] = {}
    for m in M:
        tsl = sorted(x[2] / 1000 for x in wt[m])
        if not tsl:
            continue
        res = {}
        for T in (2, 5, 20, 50):
            lost = mx = 0; t = tsl[0] + T; prev = 0
            while t <= tsl[-1] + T:
                i = bisect.bisect_right(tsl, t); k = i - prev
                mx = max(mx, k); lost += max(0, k - 200); prev = i; t += T
            res[T] = {"max_in_window": mx, "lost": lost}
        R["G"][m] = res
        P(f"  {m[4:]:5s} " + " | ".join(f"{T}초: 창 최대 {v['max_in_window']}건, 누락 {v['lost']}" for T, v in res.items()))

    P("\n[H] 체결 후 가격 변화(마크아웃) — 지정가 쪽에 유리하면 +, 단위 = 1호가.  0-: 체결 직전 중간값 기준(=그 순간 얻는 몫)")
    P("    '전부' = 맨 앞 가격에서 난 모든 체결(줄 맨 앞에 있었다는 낙관적 가정) / '소진' = 그 가격 줄이 통째로 없어진 체결만(줄 맨 뒤 가정, 보수적)")
    R["H"] = {}
    for m in M:
        o, w, tick = wo[m], wt[m], ticks.get(m)
        if not o or not w or not tick:
            continue
        ts = [x[1] for x in o]
        mids = [(x[4][0] + x[4][2]) / 2 for x in o]
        t_last = ts[-1]
        rows = []           # (tt, edge by tau..., value, wiped)
        TAUS = (1, 5, 30, 60)
        for x in w:
            tt = x[2] / 1000
            i0 = bisect.bisect_left(ts, tt) - 1
            if i0 < 0 or tt + TAUS[-1] > t_last:
                continue
            lv = o[i0][4]
            if x[4] != (lv[0] if x[6] == "ASK" else lv[2]):
                continue                                   # 맨 앞 가격에서 난 체결만 (정합이 확인된 것)
            sgn = 1 if x[6] == "ASK" else -1               # 지정가 매수자가 산 경우 +: 이후 중간값이 체결가보다 높으면 이득
            e = [sgn * (mids[i0] - x[4]) / tick]
            for tau in TAUS:
                j = bisect.bisect_right(ts, tt + tau) - 1
                e.append(sgn * (mids[j] - x[4]) / tick)
            ja = max(bisect.bisect_right(ts, tt + 0.3) - 1, 0)           # 체결 0.3초 뒤 '그 시점'의 호가에서 그 가격 줄이 사라졌는가
            la = o[ja][4]
            wiped = (la[0] < x[4]) if x[6] == "ASK" else (la[2] > x[4])
            rows.append((tt, e, x[4] * x[5], wiped))
        out = {}
        for name, sel in (("전부", rows), ("소진", [r for r in rows if r[3]])):
            if len(sel) < 5:
                out[name] = {"n": len(sel)}; continue
            E = np.array([r[1] for r in sel]); V = np.array([r[2] for r in sel]); B = np.array([int(r[0] // 60) for r in sel])
            res = {"n": len(sel), "value_krw": round(float(V.sum()))}
            for k, lab in enumerate(("0-",) + tuple(f"{t}s" for t in TAUS)):
                bm = [float(E[B == b, k].mean()) for b in sorted(set(B.tolist()))]
                res[lab] = {"mean": round(float(E[:, k].mean()), 3), "se_block": round(float(np.std(bm, ddof=1) / math.sqrt(len(bm))), 3) if len(bm) > 2 else None,
                            "vw_mean": round(float((E[:, k] * V).sum() / V.sum()), 3)}
            out[name] = res
        R["H"][m] = {"tick_pct": R["E"][m]["tick_pct"], "share_wiped": round(sum(1 for r in rows if r[3]) / len(rows), 3) if rows else None} | out
        for name in ("전부", "소진"):
            v = out[name]
            if v.get("n", 0) < 5:
                P(f"  {m[4:]:5s} {name}: n={v.get('n', 0)} (자료 부족)"); continue
            P(f"  {m[4:]:5s} {name} n={v['n']:4d}: " + " | ".join(f"{lab} {v[lab]['mean']:+.2f}±{v[lab]['se_block']} (금액가중 {v[lab]['vw_mean']:+.2f})" for lab in ("0-", "1s", "5s", "30s", "60s")))
    P("  ※ ±는 1분 묶음 평균의 표준오차. 12분 자료는 탐색용이며 채택 근거로 쓰지 않음.")

    name = os.path.join(os.path.dirname(path), "summary_" + os.path.basename(path).replace("raw_", "").replace(".json.gz", ".json"))
    json.dump(R, open(name, "w"), ensure_ascii=False, indent=1, default=str)
    P(f"\n요약 저장: {name}")


if __name__ == "__main__":
    main(sys.argv[1])
