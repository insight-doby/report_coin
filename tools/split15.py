"""하락장 급락 뒤 '3번 나눠 사기' — 15분봉으로 다시 검증 (2026-11-16).
일봉 검증(11/15)은 하루 안에서 '아래 매수 체결'과 '익절' 중 무엇이 먼저였는지 몰라 낙관/보수 두 값이 나왔다.
15분봉으로 순서를 가린다. 15분봉 하나 안에서 둘 다 닿으면 보수적으로(익절 불인정, 손절 우선).
자료: 빗썸 15분봉 2년(2024-11-16~), 코인 = 일봉 검증과 같은 23개
사례: 전날 BTC 일봉 < 20일선 & 20일선 하락(🔴), 코인 일봉 종가가 5일 전보다 -10% 이하 → 다음 날 첫 15분봉 시가에 1/3
       이후 첫 매수가 -5%, -10%에 1/3씩 지정가. 최종 손절 첫 매수가 -15%. 같은 코인은 10일 간격.
익절: 평단 +1/+2/+3/+5/+7/+10%, 그리고 '+3% 절반 → 나머지 본전 손절·+10%'. 최대 10일(+10%는 20일도).
수수료 왕복 0.08%. 손절 체결은 손절가 -0.1%.
"""
import json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opp_bot_bt as O
from datetime import datetime, timedelta
O.END = datetime(2026, 11, 16, 0, 0, tzinfo=O.KST); O.START = O.END - timedelta(days=730)
COINS = ['BTC','ETH','XRP','SOL','DOGE','ADA','LINK','ONDO','SUI','WLD','ENA','VIRTUAL','NEAR','AVAX','HBAR','XLM','PEPE','UNI','INJ','AAVE','SEI','ARB','PLUME']
FEE = 0.0008
data = {}
for c in COINS:
    try:
        data[c] = O.fetch_m15(c); print(c, len(data[c]), flush=True)
    except Exception as e:
        print(c, 'ERR', e, flush=True)
bd = O.daily(data['BTC'])
days = list(bd.index)
red = {}
for k in range(6, len(days)):
    p = days[k-1]
    red[days[k]] = bool(bd.at[p, 'c'] < bd.at[p, 'ma20'] and bd.at[p, 'ma20'] < bd.at[p, 'ma20_5'])

def run(df, i0, tp, hold_days, half=False):
    o, h, l, c = (df[k].to_numpy(float) for k in ('o', 'h', 'l', 'c'))
    e = o[i0]; lots = [e]; banked = 0.0; half_done = False
    end = min(len(df) - 1, i0 + hold_days * 96)
    for j in range(i0, end + 1):
        new = False
        if len(lots) == 1 and l[j] <= e * .95: lots.append(e * .95); new = True
        if len(lots) == 2 and l[j] <= e * .90: lots.append(e * .90); new = True
        avg = np.mean(lots); w = len(lots) / 3
        if l[j] <= e * .85:
            r = banked + w * (0.5 if half_done else 1) * (e * .85 * .999 / avg - 1); return r - FEE * w, '손절'
        if half_done and l[j] <= avg:
            return banked - FEE * w, '본전'
        if new or j == i0 and False:
            continue
        if not half:
            if h[j] >= avg * (1 + tp): return w * tp - FEE * w, '익절'
        else:
            if not half_done and h[j] >= avg * 1.03: banked = w * 0.5 * 0.03; half_done = True; continue
            if half_done and h[j] >= avg * (1 + tp): return banked + w * 0.5 * tp - FEE * w, '익절'
    avg = np.mean(lots); w = len(lots) / 3
    return banked + w * (0.5 if half_done else 1) * (c[end] / avg - 1) - FEE * w, '시간'

events = []
for cn, df in data.items():
    d = O.daily(df); dl = list(d.index); first = df.groupby(df['t'].dt.date).head(1).index
    fidx = {df.at[i, 't'].date(): i for i in first}
    last = None
    for k in range(5, len(dl) - 1):
        x, nxt = dl[k], dl[k + 1]
        if not red.get(nxt) or d.at[x, 'c'] / d.at[dl[k-5], 'c'] - 1 > -0.10: continue
        if last is not None and (x - last).days < 10: continue
        if nxt not in fidx: continue
        last = x; events.append((cn, str(nxt), fidx[nxt]))
print('사례', len(events), flush=True)
L = [f"3번 나눠 사기 · 15분봉 {O.START.date()}~{O.END.date()} · 코인 {len(data)}개 · 사례 {len(events)}건 (하락장 + 5일 -10%)",
     f"{'익절':>12s} {'보유':>4s} | {'승률':>5s} {'평균':>7s} {'중앙':>7s} | 익절/손절/본전/시간 비율 | 앞1년 평균 / 뒤1년 평균"]
split = str((O.END - timedelta(days=365)).date())
for name, tp, hd, half in [('+1%', .01, 10, False), ('+2%', .02, 10, False), ('+3%', .03, 10, False), ('+5%', .05, 10, False), ('+7%', .07, 10, False),
                           ('+10%', .10, 10, False), ('+10%', .10, 20, False), ('3%절반+10%', .10, 20, True)]:
    res = [(run(data[cn], i0, tp, hd, half), day) for cn, day, i0 in events]
    r = np.array([x[0][0] for x in res]); why = [x[0][1] for x in res]
    a = np.array([x[0][0] for x in res if x[1] < split]); b = np.array([x[0][0] for x in res if x[1] >= split])
    share = " / ".join(f"{k} {why.count(k) / len(why) * 100:.0f}%" for k in ('익절', '손절', '본전', '시간'))
    L.append(f"{name:>12s} {hd:>3d}일 | {np.mean(r > 0) * 100:4.0f}% {r.mean() * 100:+6.2f}% {np.median(r) * 100:+6.2f}% | {share} | {a.mean() * 100:+.2f}% ({len(a)}) / {b.mean() * 100:+.2f}% ({len(b)})")
txt = "\n".join(L); print(txt)
os.makedirs('data/research', exist_ok=True); open('data/research/split15.txt', 'w').write(txt + "\n")
