import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
H=bot.H4_MS
def mk(closes, vols, lows=None):
    c=np.array(closes,float); o=np.r_[c[0],c[:-1]]
    l=np.array(lows,float) if lows is not None else np.minimum(o,c)*0.995
    h=np.maximum(o,c)*1.005
    t=np.arange(len(c))*H+1_700_000_000_000
    return {"t":t.astype(float),"o":o,"h":h,"l":l,"c":c,"v":np.array(vols,float)}
# 하락 → 저점1(급락, RSI 낮음) → 반등 → 저점2(완만, 더 낮음) → 반등 3봉(거래량 붙음)
c=list(np.linspace(140,110,60))+[104,97,90,92,96,99,101,100,98,96,94,92,91,90.5,89.6,89.8,91,93,95]
v=[1000]*len(c)
low=[x*0.995 for x in c]
low[62]=88.0; low[74]=87.5   # 저점1, 저점2
for i in (75,76,77): v[i]=1400  # 반등 거래량 1.4배
v[73]=v[74]=900           # 두 번째 저점 거래량 적게
h4=mk(c,v,low)
at=h4["t"][77]+H+1
sig=bot.detect_div4h("TST",h4,at,max_age_bars=1)
print(sig)
assert sig and sig["low2"]<sig["low1"] and sig["rsi2"]>sig["rsi1"] and sig["vol_ok"]
p=bot.div4h_pick("TST",sig,95.0)
print(p["stop"],p["t1"],p["t2"],p["reasons"],p["risks"])
assert p["stop"]<95<p["t1"]<p["t2"]
# 거래량 안 붙으면 vol_ok False
v2=list(v); v2[75]=v2[76]=v2[77]=500
s2=bot.detect_div4h("TST",mk(c,v2,low),at,1); assert s2 and not s2["vol_ok"]
# 오래된 신호는 무시
assert bot.detect_div4h("TST",h4,at+5*H,1) is None
# 장부 기록 + 중복 방지
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR=td; st=bot.Store(); led=[]
    f1=bot.record_div4h(st,[p],[],led,int(at),"GO"); f2=bot.record_div4h(st,[p],[],led,int(at)+3600000,"GO")
    assert len(f1)==1 and len(f2)==0 and len(led)==1
    print(json.load(open(os.path.join(td,"div4h.json")))["signals"][0]["sig_key"])
print("DIV4H OK")
