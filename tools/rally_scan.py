"""즉석 상승장 점검: 관심 코인 24개 + 비트·이더 — 20일선 위치, 4시간 거래대금, 1시간 흐름 → data/rally_now.txt"""
import os, sys, json, subprocess, traceback, io
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "numpy"], check=False)
sys.path.insert(0, os.getcwd())
OUT = io.StringIO()
try:
    import numpy as np, bot
    cfg = json.load(open("data/brk_cfg.json"))
    at = bot.now_ms()
    lat = json.load(open("data/latest.json")).get("market", {})
    OUT.write(f"시각 {bot.iso_kst(at)} | 게이트 {lat.get('gate')} ({(json.load(open('data/latest.json')).get('generated_at') or '')[:16]}) | {[c['text'] for c in lat.get('checks', [])]}\n")
    rows = []
    for tk in ["BTC", "ETH"] + bot.brk_coins(cfg):
        d = bot.fetch_v1(tk, "1d", 40); h4 = bot.fetch_v1(tk, "4h", 30); h1 = bot.fetch_v1(tk, "1h", 30)
        if d is None or h4 is None or h1 is None:
            continue
        dc = bot.slice_completed(d, bot.DAY_MS, at)
        ma20 = float(np.mean(dc["c"][-20:])); ma10 = float(np.mean(dc["c"][-10:]))
        px = float(h1["c"][-1])
        chg24 = (px / float(h1["c"][-25]) - 1) * 100
        chg_today = (px / float(d["o"][-1]) - 1) * 100
        h4c = bot.slice_completed(h4, 4 * bot.HOUR_MS, at)
        j = len(h4c["c"]) - 1
        base = float(np.median(h4c["v"][j - 20:j]))
        vx_last = float(h4c["v"][j] / base) if base else 0
        cur_bar_v = float(h4["v"][-1]); elapsed = (at - float(h4["t"][-1])) / (4 * bot.HOUR_MS)
        vx_now = (cur_bar_v / max(elapsed, 0.1)) / base if base else 0       # 진행 중 4시간봉 거래대금 속도(평소 대비)
        m4 = float(np.mean(h4c["c"][-20:])); sd4 = float(np.std(h4c["c"][-20:]))
        h1r = [round((float(h1["c"][-k]) / float(h1["c"][-k - 1]) - 1) * 100, 1) for k in (3, 2, 1)]
        rows.append((tk, px, chg24, chg_today, (px / ma20 - 1) * 100, ma20, ma10, float(h4c["c"][j]) > ma20, vx_last, vx_now,
                     (px - m4) / (2 * sd4) if sd4 else 0, h1r, float(np.median(d["v"][-21:-1])) / 1e8))
    OUT.write(f"{'코인':8s}{'현재가':>11s}{'24h':>7s}{'오늘':>7s}{'20일선대비':>9s}{'20일선':>11s}{'10일선':>11s} 4h마감>20선 4h거래(직전/진행중) 4hBB위치 1h최근3 일거래대금(억)\n")
    for r in rows:
        OUT.write(f"{r[0]:8s}{r[1]:>11,.5g}{r[2]:>+7.1f}{r[3]:>+7.1f}{r[4]:>+8.1f}%{r[5]:>11,.5g}{r[6]:>11,.5g}  {'예' if r[7] else '아니오':4s}   {r[8]:4.1f}배/{r[9]:4.1f}배   {r[10]:+5.2f}  {r[11]} {r[12]:6.1f}\n")
except Exception:
    OUT.write(traceback.format_exc())
open("data/rally_now.txt", "w").write(OUT.getvalue())
print(OUT.getvalue())
