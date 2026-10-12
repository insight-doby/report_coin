import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import bot

HOUR, DAY = bot.HOUR_MS, bot.DAY_MS
ok_count = 0


def check(name, cond):
    global ok_count
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)
    ok_count += 1


def candles(rows, t0=0, step=HOUR):
    a = np.array([(t0 + i * step, o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(rows)], dtype=float)
    return {"t": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def entry(**kw):
    p = {"ticker": "TST", "setup": "pullback", "state": "wait", "price": 105.0, "entry": 100.0, "zone_lo": 99.0,
         "zone_hi": 101.0, "stop": 95.0, "t1": 107.5, "t2": 112.5, "score": 60}
    p.update(kw)
    e = bot.new_entry(p, 0, "GO")
    e["cursor_ts"] = 0
    e["valid_until_ts"] = 36 * HOUR
    return e


# 1) 같은 캔들에서 체결+손절 → 손절
e = entry()
cd = candles([(103, 104, 94, 96)])
bot.replay_entry(e, cd, 10 * HOUR)
check("fill+stop same candle => stop", e["status"] == "closed" and e["exit_reason"] == "stop" and e["r_net"] < -1)

# 2) 체결 후 T1, T2 순차 도달
e = entry()
cd = candles([(102, 103, 99.5, 101), (101, 108, 100, 107), (107, 113, 106, 112)])
bot.replay_entry(e, cd, 10 * HOUR)
check("fill->t1->t2", e["status"] == "closed" and e["exit_reason"] == "t2" and e["t1_hit"])
check("t2 R positive > 1.8", e["r_net"] > 1.8)

# 3) T1과 본전 이탈이 같은 캔들 → T1 캔들에서는 청산 안 하고, 다음 캔들에서 본전 처리
e = entry()
cd = candles([(102, 103, 99.5, 101), (101, 108, 99.0, 100), (100, 101, 99, 100)])
bot.replay_entry(e, cd, 10 * HOUR)
check("t1 then breakeven stop", e["exit_reason"] == "be_stop" and e["t1_hit"] and e["r_net"] > 0)

# 4) 체결 안 됨 → 만료
e = entry()
cd = candles([(105, 106, 102, 104)] * 40)
bot.replay_entry(e, cd, 100 * HOUR)
check("unfilled expires", e["status"] == "expired" and e["exit_reason"] == "unfilled")

# 5) 진행 중인 봉은 무시
e = entry()
cd = candles([(102, 103, 99.5, 101), (101, 108, 100, 107)])
bot.replay_entry(e, cd, int(1.5 * HOUR))
check("incomplete candle ignored", e["status"] == "open" and not e["t1_hit"])

# 6) 타임스톱
e = entry(state="now", price=100.0)
cd = candles([(100, 101, 99.5, 100.5)] * 130)
bot.replay_entry(e, cd, 200 * HOUR)
check("time stop", e["status"] == "closed" and e["exit_reason"] == "time")

# 7) 통계
st = bot.compute_stats([e])
check("stats overall", st["overall"]["n_closed"] == 1)

# 8) 지표 sanity
c = np.cumsum(np.ones(100)) + 100
check("rsi uptrend high", bot.rsi(c) > 90)
check("ema len", len(bot.ema(c, 20)) == 100)

# 9) 눌림목 합성 데이터 → 탐지
rng = np.random.default_rng(1)
n = 200
base = 1000 * np.exp(np.cumsum(rng.normal(0.004, 0.012, n)))
base[-8:] = base[-9] * np.linspace(1.0, 0.92, 8)  # 최근 눌림
rows = []
prev = base[0]
for x in base:
    hi, lo = max(prev, x) * 1.008, min(prev, x) * 0.992
    rows.append((prev, hi, lo, x)); prev = x
d = candles(rows, step=DAY)
cx = {"ticker": "TST", "cur": float(d["c"][-1]), "chg24": -1.0, "value24": 3e10, "d": d, "h1": None}
res = [bot.detect_pullback(cx, 2.0), bot.detect_breakout_retest(cx, 2.0)]
print("detect:", [(r["setup"], r["state"], r["plan"]) if r else None for r in res])
for r in res:
    if r:
        p = r["plan"]
        check("plan ordering", p["stop"] < p["entry"] < p["t1"] < p["t2"])
        check("stop pct in range", bot.MIN_STOP_PCT - 0.2 <= p["stop_pct"] <= bot.MAX_STOP_PCT)

# 10) 시장 신호등
def mk_ctx(trend, chg):
    c = 100 * np.exp(np.cumsum(np.full(150, trend)))
    dd = candles([(x, x * 1.01, x * 0.99, x) for x in c], step=DAY)
    return {"ticker": "X", "cur": float(c[-1]), "chg24": chg, "value24": 1e11, "d": dd, "h1": None}

up = {"BTC": mk_ctx(0.004, 1.0), **{f"A{i}": mk_ctx(0.003, 1.0) for i in range(10)}}
check("market GO", bot.compute_market(up, 0.52)["gate"] in ("GO", "SELECTIVE"))
dn = {"BTC": mk_ctx(-0.006, -5.0), **{f"A{i}": mk_ctx(-0.004, -3.0) for i in range(10)}}
check("market STOP", bot.compute_market(dn, 0.45)["gate"] == "STOP")
check("market UNKNOWN without BTC", bot.compute_market({}, None)["gate"] == "UNKNOWN")

# 11) 저장소 로컬 + sanitize
with tempfile.TemporaryDirectory() as td:
    bot.LOCAL_DATA_DIR = td
    s = bot.Store()
    s.write("a.json", {"x": float("nan"), "y": np.float64(1.5)})
    check("store roundtrip", s.read("a.json", None) == {"x": None, "y": 1.5})

# 12) 백테스트 엔진
bt = bot.backtest_coin("TST", d)
print("backtest trades:", len(bt))
check("backtest runs", isinstance(bt, list))

print(f"\nALL {ok_count} CHECKS PASSED")
