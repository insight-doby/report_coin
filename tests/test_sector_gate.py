"""섹터 과열 알림: 신호등 '매수 금지'면 매수 구간 없이 참고용 한 줄만."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot

posted = []
bot.slack_post = lambda title, blocks, **kw: posted.append(json.dumps(blocks, ensure_ascii=False))


class S:
    def __init__(self): self.d = {}
    def read(self, k, d=None): return self.d.get(k, d)
    def write(self, k, v, msg=""): self.d[k] = v


row = {"group": "밈", "n": 25, "median": 7.3, "n10": 7, "n5": 10, "hot": True, "need": 4,
       "top": [{"tk": "PEPE", "chg": 10.7, "name": "페페", "leader": True}],
       "leaders": [{"tk": "PEPE", "chg": 10.7, "name": "페페", "excess": 2.5}], "laggard_leaders": [], "value": 1}
tick = {"PEPE": {"price": 0.0062, "value": 1e9}}

for gate, want, not_want in (("STOP", "지금은 매수 안 함", "매수 구간"), ("GO", "매수 후보", "지금은 매수 안 함")):
    posted.clear()
    bot.sector_alert(S(), [dict(row)], 1_800_000_000_000, tick, ledger=[], gate=gate)
    assert want in posted[-1] and not_want not in posted[-1], (gate, posted[-1][:300])
print("OK")
