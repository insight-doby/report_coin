"""돌파 추천 시험 실행 — 실제 빗썸 데이터로 신호 감지·장부 재생이 도는지 확인 (알림 안 보냄, 저장 안 함)."""
import os, sys, tempfile, json
sys.path.insert(0, os.getcwd())
os.environ["TELEGRAM_BOT_TOKEN"] = ""; os.environ["SLACK_BOT_TOKEN"] = ""
import bot, io, contextlib
OUT = io.StringIO()
_print = print
def print(*a, **k):
    _print(*a, **k); _print(*a, file=OUT)
bot.TELEGRAM_BOT_TOKEN = ""; bot.SLACK_BOT_TOKEN = ""
src = bot.Store(); bot.LOCAL_DATA_DIR = tempfile.mkdtemp()
cfg = json.load(open("data/brk_cfg.json"))
st = bot.Store(); st.write("brk_cfg.json", cfg, "t")
at = bot.now_ms()
# 최근 30일 동안 일봉 신호가 있었던 날
for tk in bot.brk_coins(cfg):
    d = bot.fetch_v1(tk, "1d", 200)
    if d is None: print(tk, "데이터 없음"); continue
    d = bot.slice_completed(d, bot.DAY_MS, at)
    n = len(d["c"]); hits = []
    for k in range(max(130, n - 30), n):
        s = bot.brk_daily_signal(d, k)
        if s: hits.append((bot.datetime.fromtimestamp(d["t"][k] / 1000, bot.KST).strftime("%m/%d"), s))
    ma20 = bot._sma_last(d["c"], 20, n - 1)
    print(f"{tk:8s} 종가 {d['c'][-1]:>10,.4g} 20일선 {ma20:>10,.4g} ({(d['c'][-1]/ma20-1)*100:+.1f}%) 최근30일 신호 {hits}")
fresh = bot.run_breakout(st, at, "GO")
print("지금 새 신호:", [(e["ticker"], e["setup"], e["entry"]) for e in fresh])

open(os.path.join(os.getcwd(), "data", "brk_dry.txt"), "w").write(OUT.getvalue())
