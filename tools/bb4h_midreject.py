"""4시간봉 '볼린저 중간선 맞고 떨어짐' 뒤 가격 (2026-11-28, 사용자 질문: 지금 팔까?)
신호: 4시간봉 고가가 중간선(20봉 평균) 이상 닿았는데 종가는 중간선 아래, 직전 봉 종가도 중간선 아래 (아래에서 올라와 막힘)
비교: 같은 시장 상황의 아무 4시간봉
보는 것: 다음 봉 시가 대비 4시간·12시간·1일·3일 뒤 수익률, 그 사이 최저(-%)·최고(+%)
자료: 빗썸 4시간봉 약 2년, 코인 27개(돌파 목록 + BTC·LINK·PLUME), 시장: 전날 BTC < 20일선 & 20일선 하락 = STOP
"""
import json, time, os, numpy as np, pandas as pd, requests
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
D = {c: h4(c) for c in coins}
b = D["BTC"]; bd = b.groupby(b["t"].dt.date)["c"].last(); ma = bd.rolling(20).mean()
stop = {d: (bd.iloc[i-1] < ma.iloc[i-1] and ma.iloc[i-1] < ma.iloc[i-6]) for i, d in enumerate(bd.index) if i >= 26}
R = {}
for cn, d in D.items():
    o, h, l, c = (d[k].values for k in "ohlc"); mid = d["c"].rolling(20).mean().values
    for i in range(21, len(d) - 19):
        g = "STOP" if stop.get(d["t"].iloc[i].date()) else "OK"
        e = o[i+1]
        row = [c[i+1]/e-1, c[i+3]/e-1, c[i+6]/e-1, c[i+18]/e-1, l[i+1:i+7].min()/e-1, h[i+1:i+7].max()/e-1]
        if h[i] >= mid[i] and c[i] < mid[i] and c[i-1] < mid[i-1]:
            R.setdefault(("중간선 맞고 막힘", g), []).append(row)
        if i % 6 == 0:
            R.setdefault(("아무 때", g), []).append(row)
L = ["4시간 볼린저 중간선 맞고 막힌 뒤 (다음 봉 시가 기준, 중앙값 / 평균)", f"코인 {len(D)}개, 약 2년"]
for k in sorted(R):
    a = np.array(R[k]) * 100
    L.append(f"{k[0]:10s} {k[1]:4s} n={len(a):5d} | 4h {np.median(a[:,0]):+.2f}/{a[:,0].mean():+.2f} | 12h {np.median(a[:,1]):+.2f}/{a[:,1].mean():+.2f} | 1일 {np.median(a[:,2]):+.2f}/{a[:,2].mean():+.2f} ({(a[:,2]<0).mean()*100:.0f}% 하락) | 3일 {np.median(a[:,3]):+.2f}/{a[:,3].mean():+.2f} ({(a[:,3]<0).mean()*100:.0f}% 하락) | 1일 안 최저 {np.median(a[:,4]):+.2f} 최고 {np.median(a[:,5]):+.2f}")
txt = "\n".join(L); print(txt); os.makedirs("data/research", exist_ok=True); open("data/research/bb4h_midreject.txt", "w").write(txt + "\n")
