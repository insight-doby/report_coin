import sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot
H = bot.HOUR_MS; t0 = 1_800_000_000_000
def mk(closes):
    c = np.array(closes, float); o = np.r_[c[0], c[:-1]]
    return {"t": np.array([t0 + i * H for i in range(len(c))], float), "o": o, "h": np.maximum(o, c), "l": np.minimum(o, c), "c": c, "v": np.ones(len(c))}
data = {"ONDO": mk([586, 586, 585, 584, 586, 592, 613]),      # 1시간 +5% 급등 (10/20 23시와 비슷)
        "AAVE": mk([210000, 210000, 209000, 205000, 202000, 200000, 199800]),  # 고점 대비 -5% 반납
        "LINK": mk([16300] * 7)}
bot.fetch_v1 = lambda tk, tf, n=8: data.get(tk)
sent = []
bot.slack_post = lambda text, blocks, thread_ts=None, **kw: sent.append(blocks) or "1"
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    json.dump({"positions": [{"ticker": "ONDO", "avg": 575, "style": "long"}, {"ticker": "AAVE", "avg": 190000, "style": "swing"},
                             {"ticker": "LINK", "avg": 17040, "style": "long"}]}, open(os.path.join(td, "positions.json"), "w"))
    s = bot.Store()
    m = bot.run_position_watch(s, t0 + 7 * H)
    assert any("ONDO" in x and "급등" in x for x in m), m
    assert any("AAVE" in x and "반납" in x for x in m), m
    assert not any("LINK" in x for x in m)
    assert bot.run_position_watch(s, t0 + 7 * H + 600000) == []          # 쿨다운
    print("POS OK", len(m))
