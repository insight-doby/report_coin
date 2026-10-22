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

# 선별 매수도 참고용
posted.clear()
bot.sector_alert(S(), [dict(row)], 1_800_000_000_000, tick, ledger=[], gate="SELECTIVE")
assert "지금은 매수 안 함" in posted[-1] and "매수 구간" not in posted[-1], posted[-1][:300]

# 성적이 나쁘면 '매수 가능'이어도 참고용
st = S(); st.d["stats.json"] = {"by_setup": {"sector_lead": {"n_closed": 8, "win_rate": 12.5}}}
posted.clear()
bot.sector_alert(st, [dict(row)], 1_800_000_000_000, tick, ledger=[], gate="GO")
assert "최근 8건 중 1승" in posted[-1] and "매수 구간" not in posted[-1], posted[-1][:300]

# 1시간 급등: 매수 가능일 때만 손절·목표
mv = [{"ticker": "ENA", "kind": "move", "bar": "2026-10-22T00:00:00+09:00", "close": 294.0, "chg1h": 5.8, "vol_x": 4.6,
       "high": 297.0, "low": 279.0, "trigger": None, "setup_label": None, "uptrend": True}]
posted.clear(); bot.slack_moves(mv, "SELECTIVE")
assert "따라 사지 않음" in posted[-1] and "목표 +15~25%" not in posted[-1]
posted.clear(); bot.slack_moves(mv, "GO")
assert "목표 +15~25%" in posted[-1]

# 스캔 추천: 눌림목 성적이 나쁘면 참고용으로
latest = {"market": {"gate": "GO"}, "picks": [{"ticker": "DOT", "setup": "pullback", "setup_label": "눌림목", "price": 1547}]}
buy, ref = bot.split_picks(latest, {"by_setup": {"pullback": {"n_closed": 25, "win_rate": 8.0}}})
assert not buy and "최근 25건 중 2승" in ref[0][1], ref
buy, ref = bot.split_picks(latest, {"by_setup": {"pullback": {"n_closed": 25, "win_rate": 48.0}}})
assert buy and not ref
print("OK2")

# 거래대금 5억 미만은 알림에서 빠짐
posted.clear()
bot.sector_alert(S(), [dict(row)], 1_800_000_000_000, {"PEPE": {"price": 0.0062, "value": 3e8}}, ledger=[], gate="GO")
assert "PEPE" not in "".join(x for x in posted if "매수 후보" in x), posted
latest = {"market": {"gate": "GO"}, "picks": [{"ticker": "W", "setup": "pullback", "setup_label": "눌림목", "price": 20, "value24": 4e8}]}
assert bot.split_picks(latest, {}) == ([], [])
print("OK3")
