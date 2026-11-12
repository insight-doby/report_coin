"""E3 — '기회 오면 들어가서 +1% 먹고 나오는' 봇, 과거 2년 검증 (공개 시세만, 실주문 없음).

규칙은 2026-11-12 실행 전에 고정. 결과를 본 뒤 바꾸면 새 번호로 기록한다.

자료: 빗썸 15분봉, 2년 (2024-11-12 ~ 2026-11-12), 코인 = data/brk_cfg.json 의 24개 + BTC (BTC 는 시장 판단과 매매 둘 다)
  일봉·4시간봉은 15분봉으로 만든다(일봉 = KST 자정 기준).

기회(신호) — 이미 검증했던 신호를 15분 해상도로 옮긴 것. 판단은 15분봉 마감, 매수는 다음 봉 시가.
  S1 하락장 급락 줍기 : 전날 기준 BTC 일봉 종가 < BTC 20일선 이고 20일선이 5일 전보다 낮음(시장 신호 🔴),
                        그리고 지금 가격 ≤ 최근 5일 종가 최고치 × 0.90
  S2 일봉 20일선 돌파 : 전날 일봉 종가 > 20일선, 그 전날 종가 ≤ 20일선 → 오늘 첫 15분봉 시가에 매수
  S3 4시간 거래량 돌파: 4시간봉 종가가 전날 기준 일봉 20일선 위로 올라섬(직전 4시간봉은 아래) + 그 4시간봉 거래량 ≥ 직전 20개 평균의 3배
  S4 (탐색용, 검증된 적 없음) 1시간 급락 후 첫 양봉: 최근 1시간 수익률 ≤ -3% 이고 방금 15분봉이 양봉

나가기: 익절 +1% (지정가). 손절 1%/2%/3%, 최대 보유 24시간/72시간 — 6가지 조합 전부 보고.
  · 한 봉 안에서 손절가와 익절가가 둘 다 닿으면 손절이 먼저라고 본다(보수적)
  · 매수는 다음 봉 시가 + 0.05% 미끄러짐, 손절은 손절가 − 0.10% 미끄러짐, 익절은 익절가가 0.05% 넘게 뚫려야 체결로 인정
  · 최대 보유 시간이 되면 그 봉 종가에 판다
  · 수수료: 0원(API 무료 가정) 과 한쪽 0.04% 두 가지로 보고
  · 같은 코인은 한 번에 1건. 나간 뒤 4시간은 같은 신호로 다시 안 들어감
판정 기준
  · 앞 1년(개발)과 뒤 1년(검증)을 따로 본다. 둘 다 거래 30건 이상 + 평균 수익 > 0 (수수료 0.04% 포함) 인 조합만 '모의 2주 후보'
  · 거래 100건 중 상위 5건을 빼도 + 인지, 한 코인이 수익의 절반 넘게 차지하지 않는지 함께 본다
출력: data/research/opp_bt.json, data/research/opp_bt.txt
"""
import json, os, sys, time, traceback
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
B = "https://api.bithumb.com/v1"
END = datetime(2026, 11, 12, 0, 0, tzinfo=KST)
START = END - timedelta(days=730)
SPLIT = END - timedelta(days=365)
SL_LIST, HOLD_LIST, TP = (0.01, 0.02, 0.03), (24, 72), 0.01
SLIP_IN, SLIP_SL, TP_PASS = 0.0005, 0.0010, 0.0005
COOL_H = 4
S = requests.Session(); S.headers["User-Agent"] = "report-coin-research"


def fetch_m15(coin):
    rows, to = [], END.strftime("%Y-%m-%dT%H:%M:%S")
    for _ in range(600):
        for att in range(4):
            try:
                r = S.get(f"{B}/candles/minutes/15", params={"market": f"KRW-{coin}", "count": 200, "to": to}, timeout=15)
                j = r.json() if r.status_code == 200 else None
                break
            except Exception:
                j = None; time.sleep(1 + att)
        if not isinstance(j, list) or not j:
            break
        rows += [(x["candle_date_time_kst"], x["opening_price"], x["high_price"], x["low_price"], x["trade_price"], x["candle_acc_trade_price"]) for x in j]
        oldest = j[-1]["candle_date_time_kst"]
        if oldest <= START.strftime("%Y-%m-%dT%H:%M:%S"):
            break
        to = oldest
        time.sleep(0.04)
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"]).drop_duplicates("t").sort_values("t").reset_index(drop=True)
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(KST)
    return df[df["t"] >= START].reset_index(drop=True)


def daily(df):
    d = df.groupby(df["t"].dt.date).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))
    d["ma20"] = d["c"].rolling(20).mean()
    d["ma20_5"] = d["ma20"].shift(5)
    return d


def signals(df, d, btc_d):
    """각 15분봉 마감 시점의 신호 (True/False 배열 4개)."""
    n = len(df)
    date = df["t"].dt.date.to_numpy()
    prev = {k: v for k, v in zip(d.index, d.index.to_series().shift(1))}           # 날짜 → 전날
    dc = d["c"].to_dict(); dma = d["ma20"].to_dict()
    bc = btc_d["c"].to_dict(); bma = btc_d["ma20"].to_dict(); bma5 = btc_d["ma20_5"].to_dict()
    c = df["c"].to_numpy(); o = df["o"].to_numpy(); v = df["v"].to_numpy()
    # S1
    roll_hi = pd.Series(c).rolling(5 * 96, min_periods=96).max().shift(1).to_numpy()  # 최근 5일(15분봉 480개) 최고 종가
    stop = np.array([(lambda p: p is not None and p in bc and bma.get(p) == bma.get(p) and bc[p] < bma[p] and bma5.get(p) == bma5.get(p) and bma[p] < bma5[p])(prev.get(x)) for x in date])
    s1 = stop & (c <= roll_hi * 0.90)
    # S2: 그날 첫 봉에서만
    first = np.r_[True, date[1:] != date[:-1]]
    def cross_up(x):
        p = prev.get(x); pp = prev.get(p) if p is not None else None
        return p is not None and pp is not None and dma.get(p) == dma.get(p) and dma.get(pp) == dma.get(pp) and dc[p] > dma[p] and dc[pp] <= dma[pp]
    s2_day = {x: cross_up(x) for x in set(date)}
    # S2 는 '그날 첫 봉 시가 매수' 이므로 신호를 전날 마지막 봉 마감에 둔다 → 다음 봉(그날 첫 봉) 시가 매수
    s2 = np.zeros(n, bool)
    idx_first = np.where(first)[0]
    for i in idx_first:
        if i > 0 and s2_day.get(date[i]):
            s2[i - 1] = True
    # S3: 4시간봉
    g = (df["t"].astype("int64") // (4 * 3600 * 10**9)).to_numpy()
    last_in_g = np.r_[g[1:] != g[:-1], True]
    h4 = df.groupby(g).agg(c=("c", "last"), v=("v", "sum"))
    h4["vavg"] = h4["v"].rolling(20).mean().shift(1)
    h4["c_prev"] = h4["c"].shift(1)
    h4map = h4.to_dict("index")
    s3 = np.zeros(n, bool)
    for i in np.where(last_in_g)[0]:
        r = h4map[g[i]]; p = prev.get(date[i])
        if p is None or dma.get(p) != dma.get(p) or r["vavg"] != r["vavg"] or r["c_prev"] != r["c_prev"]:
            continue
        if r["c"] > dma[p] and r["c_prev"] <= dma[p] and r["v"] >= 3 * r["vavg"]:
            s3[i] = True
    # S4
    r1h = c / np.r_[np.full(4, np.nan), c[:-4]] - 1
    s4 = (r1h <= -0.03) & (c > o)
    return {"S1": s1, "S2": s2, "S3": s3, "S4": s4}


def run_trades(df, sig, sl, hold_h, coin, regime):
    t = df["t"].to_numpy(); o = df["o"].to_numpy(); h = df["h"].to_numpy(); l = df["l"].to_numpy(); c = df["c"].to_numpy()
    n = len(df); hold = hold_h * 4; cool = COOL_H * 4
    out = []; i = 0; next_ok = 0
    idx = np.where(sig)[0]
    for i in idx:
        if i < next_ok or i + 1 >= n:
            continue
        e = o[i + 1] * (1 + SLIP_IN)
        tp, sp = e * (1 + TP), e * (1 - sl)
        exit_px, reason, j = None, None, None
        for j in range(i + 1, min(n, i + 1 + hold)):
            if l[j] <= sp:
                exit_px, reason = sp * (1 - SLIP_SL), "손절"; break
            if h[j] >= tp * (1 + TP_PASS):
                exit_px, reason = tp, "익절"; break
        if exit_px is None:
            j = min(n - 1, i + hold)
            exit_px, reason = c[j], "시간"
        out.append({"coin": coin, "entry_t": str(pd.Timestamp(t[i + 1]).tz_convert(KST))[:16], "ret": exit_px / e - 1, "reason": reason,
                    "bars": int(j - i), "regime": regime.get(pd.Timestamp(t[i]).tz_convert(KST).date(), "?"),
                    "test": bool(pd.Timestamp(t[i + 1]) >= SPLIT)})
        next_ok = j + cool
    return out


def btc_regime(btc_d):
    r = {}
    days = list(btc_d.index)
    for k, x in enumerate(days[1:], 1):
        p = days[k - 1]
        c, ma, ma5 = btc_d.at[p, "c"], btc_d.at[p, "ma20"], btc_d.at[p, "ma20_5"]
        if ma != ma or ma5 != ma5:
            continue
        r[x] = "상승" if c > ma and ma > ma5 else "하락" if c < ma and ma < ma5 else "횡보"
    return r


def stats(tr, fee):
    if not tr:
        return {"n": 0}
    r = np.array([x["ret"] for x in tr]) - 2 * fee
    srt = np.sort(r)
    by_coin = {}
    for x, rr in zip(tr, r):
        by_coin[x["coin"]] = by_coin.get(x["coin"], 0) + rr
    top_coin = max(by_coin.items(), key=lambda kv: kv[1]) if by_coin else (None, 0)
    pos_sum = sum(v for v in by_coin.values() if v > 0)
    return {"n": int(r.size), "win": round(float((r > 0).mean()) * 100, 1), "avg_pct": round(float(r.mean()) * 100, 3), "sum_pct": round(float(r.sum()) * 100, 1),
            "worst_pct": round(float(r.min()) * 100, 2), "tp_share": round(100 * sum(1 for x in tr if x["reason"] == "익절") / len(tr), 1),
            "sl_share": round(100 * sum(1 for x in tr if x["reason"] == "손절") / len(tr), 1),
            "avg_wo_top5_pct": round(float(srt[:-5].mean()) * 100, 3) if r.size > 10 else None,
            "top_coin": top_coin[0], "top_coin_share": round(100 * top_coin[1] / pos_sum, 1) if pos_sum > 0 else None,
            "per_year": round(r.size / 2, 1)}


def main():
    cfg = json.load(open("data/brk_cfg.json"))
    coins = ["BTC"] + [x for k in ("swing", "large", "small") for x in cfg.get(k, [])]
    data = {}
    for cn in coins:
        try:
            df = fetch_m15(cn)
            data[cn] = df
            print(cn, len(df), df["t"].iloc[0] if len(df) else None, flush=True)
        except Exception:
            print(cn, "ERROR", traceback.format_exc()[-200:], flush=True)
    btc_d = daily(data["BTC"])
    reg = btc_regime(btc_d)
    trades = {}
    for cn, df in data.items():
        if len(df) < 96 * 60:
            continue
        d = daily(df)
        sg = signals(df, d, btc_d)
        for s, arr in sg.items():
            for sl in SL_LIST:
                for hh in HOLD_LIST:
                    trades.setdefault((s, sl, hh), []).extend(run_trades(df, arr, sl, hh, cn, reg))
    res = {"rules": "E3-2026-11-12", "period": [str(START.date()), str(END.date())], "split": str(SPLIT.date()), "coins": list(data), "bars": {k: len(v) for k, v in data.items()},
           "git": os.popen("git rev-parse HEAD").read().strip(), "combos": {}}
    L = [f"E3 +1% 기회 봇 · 15분봉 {res['period'][0]}~{res['period'][1]} · 코인 {len(data)}개 · 앞 1년=개발 / 뒤 1년=검증",
         "신호  손절 보유 | 수수료0: 개발(건수 승률 평균%) / 검증(건수 승률 평균%) | 수수료0.04%: 개발 평균 / 검증 평균 | 검증: 익절·손절 비율, 상위5 빼고 평균, 최대기여 코인 | 판정"]
    for (s, sl, hh), tr in sorted(trades.items()):
        dev = [x for x in tr if not x["test"]]; tst = [x for x in tr if x["test"]]
        st = {"dev0": stats(dev, 0), "test0": stats(tst, 0), "dev4": stats(dev, 0.0004), "test4": stats(tst, 0.0004),
              "test0_by_regime": {g: stats([x for x in tst if x["regime"] == g], 0) for g in ("상승", "하락", "횡보")}}
        ok = all(st[k]["n"] >= 30 and st[k]["avg_pct"] > 0 for k in ("dev4", "test4"))
        st["candidate"] = ok
        res["combos"][f"{s}|{sl}|{hh}"] = st
        a, b, a4, b4 = st["dev0"], st["test0"], st["dev4"], st["test4"]
        f = lambda z: f"{z['n']:4d} {z.get('win', 0):5.1f}% {z.get('avg_pct', 0):+.3f}" if z["n"] else "   0      -      -"
        L.append(f"{s}  {int(sl * 100)}%  {hh:2d}h | {f(a)} / {f(b)} | {a4.get('avg_pct', 0):+.3f} / {b4.get('avg_pct', 0):+.3f} | "
                 f"{b.get('tp_share', 0)}%·{b.get('sl_share', 0)}%, {b.get('avg_wo_top5_pct')}, {b.get('top_coin')} {b.get('top_coin_share')}% | {'★후보' if ok else ''}")
    L.append("")
    L.append("검증 1년, 수수료 0, 시장 상황별 (건수 승률 평균%):")
    for k, st in res["combos"].items():
        if k.endswith("|24") and "|0.02|" in k:
            L.append(f"  {k}: " + " / ".join(f"{g} {v['n']}건 {v.get('win', 0)}% {v.get('avg_pct', 0):+.3f}" for g, v in st["test0_by_regime"].items()))
    json.dump(res, open("data/research/opp_bt.json", "w"), ensure_ascii=False, indent=1, default=str)
    txt = "\n".join(L)
    open("data/research/opp_bt.txt", "w").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
