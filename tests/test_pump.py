import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
DAY=bot.DAY_MS
def cd(closes, highs=None, lows=None):
    c=np.array(closes,float); o=np.r_[c[0],c[:-1]]
    h=np.array(highs,float) if highs is not None else np.maximum(o,c)*1.01
    l=np.array(lows,float) if lows is not None else np.minimum(o,c)*0.99
    return {"t":np.arange(len(c))*DAY*1.0,"o":o,"h":h,"l":l,"c":c,"v":np.full(len(c),1000.0)}
# 40일 횡보 100 → 2일 급등 150 → 하락 110(-27%) → 반등 125
cl=[100]*40+[125,150,135,118,110,116,125,124,123,122,121,120,120,120]
d=cd(cl)
ev=bot.collect_pump_events("TST",d)
assert len(ev)==1, ev
e=ev[0]; print("event", e["date"], e["pump_pct"], "pump_days", e["pump_days"], "path len", len(e["path"]))
r=bot.sim_pump_dip(e,20,12,3,5); print("dip20 stop12 tgt3:",r)
assert r and r["why"]=="target" and 2.8<r["ret"]<3.1 and r["days_to_fill"]==2
r=bot.sim_pump_dip(e,30,8,None,5); print("dip30:",r)
s=bot.summarize_pump(ev); print("reach",s["reach_within_7d"],"base",s["back_to_base_within_7d_pct"],"grid",len(s["grid"]))
# 하락 지속 케이스 → 손절
cl2=[100]*40+[130,160,140,120,100,85,80,78,77,76,75,75,75,75]
e2=bot.collect_pump_events("T2",cd(cl2))[0]
r=bot.sim_pump_dip(e2,25,10,15,5); print("falling knife:",r); assert r["why"]=="stop" and r["ret"]<-9
# 실시간 관찰
cx={"ticker":"TST","cur":118.0,"chg24":-5,"value24":1e10,"d":cd(cl[:44]),"h1":None}
lp=bot.live_pumps({"TST":cx}); print("live",lp); assert lp and lp[0]["from_peak_pct"]<-15
print("PUMP OK")
