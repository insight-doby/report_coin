"""4시간봉 볼린저 '하단 아래 → 다시 하단 위로 마감' 매수 검증 (2026-11-15, 사용자 질문).
기존 검증은 '일봉 하단 터치(내려가는 중)'였고, '아래로 빠졌다가 다시 올라오는 순간'은 따로 본 적 없음.
자료: 빗썸 4시간봉 2년, 코인 = 돌파 목록 24개 + BTC·LINK·PLUME
신호: 직전 4시간봉 종가 < 하단(20,2), 이번 4시간봉 종가 > 하단 → 다음 봉 시가 매수(+0.05% 미끄러짐)
비교: (가) 내려가는 중 하단 아래 마감(기존 방식)  (기준) 아무 4시간봉
나가기 A: +3% 익절 / -3% 손절 / 최대 3일   B: 중간선 도달 익절 / 신호봉 저점 -1% 손절 / 최대 3일
한 봉에 둘 다 닿으면 손절. 수수료 왕복 0.08%. 시장: 전날 BTC 일봉 < 20일선 & 20일선 하락 = STOP
"""
import json, time, numpy as np, pandas as pd, requests
B = "https://api.bithumb.com/v1"; S = requests.Session()
cfg = json.load(open("data/brk_cfg.json"))
coins = ["BTC", "LINK", "PLUME"] + [x for k in ("swing", "large", "small") for x in cfg[k]]
def h4(c):
    rows, to = [], None
    for _ in range(25):
        p = {"market": f"KRW-{c}", "count": 200}
        if to: p["to"] = to
        j = S.get(f"{B}/candles/minutes/240", params=p, timeout=15).json()
        if not isinstance(j, list) or not j: break
        rows += [(x["candle_date_time_kst"], x["opening_price"], x["high_price"], x["low_price"], x["trade_price"]) for x in j]
        to = j[-1]["candle_date_time_kst"]; time.sleep(0.05)
    d = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"]).drop_duplicates("t").sort_values("t").reset_index(drop=True)
    d["t"] = pd.to_datetime(d["t"]); return d
D = {}
for c in coins:
    try: D[c] = h4(c); print(c, len(D[c]), D[c]["t"].iloc[0], flush=True)
    except Exception as e: print(c, "ERR", e)
b = D["BTC"]; bd = b.groupby(b["t"].dt.date)["c"].last(); ma = bd.rolling(20).mean()
stop = {d: (bd.iloc[i - 1] < ma.iloc[i - 1] and ma.iloc[i - 1] < ma.iloc[i - 6]) for i, d in enumerate(bd.index) if i >= 26}
FEE = 0.0008
def trade(d, i, mode, mid):
    o, h, l, c = d["o"].values, d["h"].values, d["l"].values, d["c"].values
    if i + 1 >= len(d): return None
    e = o[i + 1] * 1.0005
    if mode == "A": tp, sl = e * 1.03, e * 0.97
    else:
        tp, sl = mid[i], l[i] * 0.99
        if tp <= e * 1.003 or sl >= e: return None
    for j in range(i + 1, min(len(d), i + 1 + 18)):
        if l[j] <= sl: return sl / e - 1 - FEE
        if h[j] >= tp: return tp / e - 1 - FEE
    return c[min(len(d) - 1, i + 18)] / e - 1 - FEE
R = {}
for cn, d in D.items():
    c = d["c"]; m = c.rolling(20).mean(); s = c.rolling(20).std(); lo = (m - 2 * s).values; mid = m.values; cv = c.values
    for i in range(21, len(d) - 1):
        g = "STOP" if stop.get(d["t"].iloc[i].date()) else "OK"
        sig = []
        if cv[i - 1] < lo[i - 1] and cv[i] > lo[i]: sig.append("재진입(아래→위)")
        if cv[i] < lo[i] and cv[i] < cv[i - 1]: sig.append("내려가는 중 하단 아래")
        if i % 6 == 0: sig.append("기준(아무 때)")
        for sname in sig:
            for mode in ("A", "B"):
                r = trade(d, i, mode, mid)
                if r is not None: R.setdefault((sname, mode, g), []).append((r, cn, str(d["t"].iloc[i])[:10]))
L = ["4시간 볼린저 하단 재진입 검증 · 빗썸 4시간봉 약 2년 · 코인 %d개 · 수수료 왕복 0.08%%" % len(D),
     "A=+3%/-3%/3일, B=중간선 익절/신호봉 저점-1% 손절/3일"]
for k in sorted(R):
    v = np.array([x[0] for x in R[k]]); half = len(v) // 2
    L.append(f"{k[0]:14s} {k[1]} {k[2]:4s} | {len(v):5d}건 승률 {np.mean(v > 0) * 100:4.0f}% 평균 {v.mean() * 100:+.2f}% | 앞절반 {v[:half].mean() * 100:+.2f}% 뒤절반 {v[half:].mean() * 100:+.2f}%")
on = [x for k in R if k[0] == "재진입(아래→위)" and k[1] == "A" for x in R[k] if x[1] == "ONDO"]
if on:
    v = np.array([x[0] for x in on]); L.append(f"온도만 재진입 A: {len(v)}건 승률 {np.mean(v > 0) * 100:.0f}% 평균 {v.mean() * 100:+.2f}%")
txt = "\n".join(L); print(txt)
import os; os.makedirs("data/research", exist_ok=True); open("data/research/bb4h_reentry.txt", "w").write(txt + "\n")
