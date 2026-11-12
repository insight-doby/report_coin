"""E2d — 똑똑한 봇 vs 단순 봇, 과거 호가창+체결 이틀치 (Tardis 무료 공개분, E2a·E2b 와 같은 날·같은 종목 규칙).

엔진: tools/paper_mm.py (규칙 pm1, 2026-11-12 고정). 수신 지연 0.15초 + 주문 지연 0.1초, 수수료 0.
판정 기준 (실행 전 고정)
  · E2b 와 같은 7개 후보에서 똑똑한 봇 '보통' 합계가 + 이고 5개 이상 + → 2주 실시간 모의 대상
  · 합계는 − 지만 단순 봇보다 손실이 절반 이하로 줄면 → 규칙을 한 번 더 고쳐 볼 가치 있음(보류)
  · 그 밖 → 똑똑한 봇도 탈락
  · 나머지 13개는 참고로 함께 보고(판정에 쓰지 않음)
사용: python tools/mm_paper_hist.py 2026-10-01 2026-11-01
"""
import json, os, sys, time, traceback
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mm_queue_sim as Q
import paper_mm as P

CANDS = {"2026-10-01": ["KRW-WLD", "KRW-ONDO", "KRW-ENA"], "2026-11-01": ["KRW-WLD", "KRW-LSK", "KRW-XRP", "KRW-USDT"]}
SCEN = {"보수": 0.0, "보통": None, "낙관": 1.0}


def main(days):
    out = {"rules": P.RULES_VERSION, "git": os.popen("git rev-parse HEAD").read().strip(), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": {}}
    for day in days:
        src = json.load(open(f"data/research/tardis_{day}.json"))
        D = out["days"][day] = {}
        for sym in src["selected"]:
            try:
                tr, ob = Q.load(day, sym)
                if tr is None or len(tr) < 50:
                    D[sym] = {"error": "자료 없음"}; continue
                res = {"candidate": sym in CANDS.get(day, []), "runs": {}}
                for v in ("단순", "똑똑"):
                    for name, a in SCEN.items():
                        t0 = time.time()
                        r = P.simulate(tr, ob, v, a)
                        r["sec"] = round(time.time() - t0, 1)
                        res["runs"][f"{v}|{name}"] = r
                D[sym] = res
                print(day, sym, {k: v["pnl_krw"] for k, v in res["runs"].items()}, flush=True)
            except Exception:
                D[sym] = {"error": traceback.format_exc()[-600:]}
                print(day, sym, "ERROR", traceback.format_exc()[-300:], flush=True)
    json.dump(out, open("data/research/mm_paper_hist.json", "w"), ensure_ascii=False, indent=1, default=float)
    L = ["E2d 똑똑한 봇 vs 단순 봇 (원, 하루 손익, 처분 포함, 30만원 단위, 최대 보유 60만원)",
         f"{'날짜':6s}{'코인':8s}{'후보':>4s} | {'단순 보수':>10s}{'단순 보통':>10s}{'단순 낙관':>10s} | {'똑똑 보수':>10s}{'똑똑 보통':>10s}{'똑똑 낙관':>10s} | 똑똑보통: 매수/매도 체결, 빠지기, 쉬기"]
    tot = {}
    for day, D in out["days"].items():
        for sym, r in D.items():
            if "error" in r:
                L.append(f"{day[5:]:6s}{sym[4:]:8s} {r['error'][:60]}"); continue
            g = lambda k: r["runs"][k]["pnl_krw"]
            for k in r["runs"]:
                tot.setdefault(("후보" if r["candidate"] else "기타", k), 0)
                tot[("후보" if r["candidate"] else "기타", k)] += g(k)
            m = r["runs"]["똑똑|보통"]
            L.append(f"{day[5:]:6s}{sym[4:]:8s}{'●' if r['candidate'] else '':>4s} | {g('단순|보수'):>10,d}{g('단순|보통'):>10,d}{g('단순|낙관'):>10,d} | {g('똑똑|보수'):>10,d}{g('똑똑|보통'):>10,d}{g('똑똑|낙관'):>10,d} | {m['fills_buy']}/{m['fills_sell']}, {m['pulls']}, {m['pauses']}")
    for grp in ("후보", "기타"):
        L.append(f"합계({grp})      | " + "".join(f"{tot.get((grp, k), 0):>10,d}" for k in ("단순|보수", "단순|보통", "단순|낙관")) + " | " + "".join(f"{tot.get((grp, k), 0):>10,d}" for k in ("똑똑|보수", "똑똑|보통", "똑똑|낙관")))
    txt = "\n".join(L)
    open("data/research/mm_paper_hist.txt", "w").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if a[:2] == "20"] or ["2026-10-01", "2026-11-01"])
