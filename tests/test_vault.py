import sys, os, tempfile, json, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, bot, vault
H = bot.HOUR_MS; t0 = 1_800_000_000_000
def mk(closes):
    c = np.array(closes, float); o = np.r_[c[0], c[:-1]]
    return {"t": np.array([t0 + i * H for i in range(len(c))], float), "o": o, "h": np.maximum(o, c), "l": np.minimum(o, c), "c": c, "v": np.ones(len(c))}
bot.fetch_v1 = lambda tk, tf, n=8: {"AAVE": mk([200000] * 6 + [205000, 216000]), "LINK": mk([16300] * 8)}.get(tk)
tg, sl = [], []
bot.telegram_send = lambda t, html=False: tg.append(t) or True
bot.slack_post = lambda *a, **k: sl.append(a) or "1"
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td; bot.ACCOUNT_PASSWORD = "test-password-12345"
    json.dump({"positions": [{"ticker": "AAVE", "avg": 199700, "style": "swing", "stop": 195000, "t1": 215000},
                             {"ticker": "LINK", "avg": 17040, "style": "long"}]}, open(os.path.join(td, "positions.json"), "w"))
    json.dump({"AAVE": {"peak": 210400}}, open(os.path.join(td, "positions_state.json"), "w"))
    json.dump({"watch": ["BTC"], "notes": {"LINK": "평단 17,040원"}}, open(os.path.join(td, "accumulate_cfg.json"), "w"))
    json.dump({"alerts": [{"id": "AAVE-pos-t1", "ticker": "AAVE", "position": {"entry": 199700}, "status": "active"},
                          {"id": "BTC-x", "ticker": "BTC", "status": "active"}]}, open(os.path.join(td, "price_alerts.json"), "w"))
    bot.run_vault_init()
    assert not os.path.exists(os.path.join(td, "positions.json")) and not os.path.exists(os.path.join(td, "positions_state.json"))
    assert [a["id"] for a in json.load(open(os.path.join(td, "price_alerts.json")))["alerts"]] == ["BTC-x"]
    assert "notes" not in json.load(open(os.path.join(td, "accumulate_cfg.json")))
    vault.add_event(td, {"op": "add_trade", "trade": {"ticker": "ADA", "side": "sell", "price": 296}})   # 비밀번호 없이 추가
    m = bot.run_position_watch(bot.Store(), t0 + 8 * H)
    assert any("1차 목표" in x for x in m) and tg and not sl, (m, sl)          # 금고 모드: 텔레그램으로만
    blob = "".join(open(f).read() for f in glob.glob(os.path.join(td, "**", "*.json"), recursive=True))
    for leak in ("199700", "199,700", "17040", "17,040", "AAVE\""):
        assert leak not in blob, leak                                             # 공개 파일 어디에도 평단·보유 종목 없음
    snap, files = vault.load(td, "test-password-12345")
    assert files == [] and snap["trades"][0]["ticker"] == "ADA" and snap["state"]["AAVE"]["t1_hit"]
    bot.ACCOUNT_PASSWORD = ""
    assert bot.run_position_watch(bot.Store(), t0 + 9 * H) == []                 # 비밀번호 없으면 건너뜀 (공개 파일로 되돌아가지 않음)
    assert not os.path.exists(os.path.join(td, "positions.json"))
print("VAULT OK")
