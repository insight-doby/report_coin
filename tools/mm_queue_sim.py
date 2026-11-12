"""E2b — '줄 서서 기다리기' 재생: 과거 호가창+체결로 단순 양쪽 지정가 매매를 하루 동안 돌렸다면.

자료: Tardis.dev 무료 공개분(매달 1일) 빗썸 trades + book_snapshot_5 (E2a 와 같음).
종목: 날마다 전날(UTC) 빗썸 일봉 거래대금 상위 10개 (E2a 와 같은 규칙, 같은 목록).

전략 (실행 전에 고정)
  · 한 번에 주문 1개씩, 크기 = 그날 첫 가격 기준 30만원어치(LOT)
  · 매수: 보유 < MAX_LOTS 이면 매수 맨 앞 가격(bid1)에 건다
  · 매도: 보유 ≥ 1 LOT 이면 매도 맨 앞 가격(ask1)에 1 LOT 건다 (현물이라 공매도 없음)
  · 내 가격보다 더 좋은 가격이 맨 앞에 생기면 취소 후 새 맨 앞 가격에 다시 건다
  · 주문·취소는 결정 후 LAT 초 뒤에 효력. 취소가 효력을 내기 전에 들어온 체결은 그대로 받는다
  · 하루가 끝나면 남은 물량을 매수 호가 5칸을 차례로 쓸어 처분(못 다 팔면 5번째 칸 가격 −1%로 가정)
  · 수수료 0원

줄 위치 (공개 자료에 없는 부분 → 가정)
  · 주문이 효력을 내는 순간, 그 가격에 이미 보이는 물량 '뒤'에 선다
  · 그 가격에서 일어난 체결은 내 앞줄부터 줄인다 (기록에 있음)
  · 호가 변화에서 '체결로 설명되지 않는 감소'를 취소로 본다. 취소가 내 앞에서 일어난 비율:
      보수 = 0 (전부 내 뒤에서 취소)      보통 = 내 앞 물량 / 그 가격 전체 물량      낙관 = 1 (전부 내 앞에서 취소)
  · 그 가격 줄이 통째로 사라지면 남은 내 주문이 맨 앞 혼자가 된다(앞줄 0)
  · 내 가격보다 불리한 가격의 체결(매수 주문이면 더 낮은 가격의 매도 체결)은 내 가격 줄이 다 쓸렸다는 뜻 → 남은 양 전부 체결

판정 기준 (실행 전에 고정, 2026-11-12)
  · 대상: E2a 에서 '줄 앞'은 +, '줄 뒤'는 − 였던 7개 (월드코인 10/1·11/1, 온도 10/1, 에테나 10/1, 리스크 11/1, 리플 11/1, 테더 11/1)
  · '보통·지연 0.15초'에서 7개 중 5개 이상이 + 이고 합계가 + → 다음 단계(소액 실측) 후보
  · 위를 만족해도 '보수' 합계가 − 이거나 지연 0.5초에서 합계가 − 이면 → 보류(줄 순서·속도에 달림)
  · '보통·지연 0.15초' 합계가 − → 단순 양쪽 지정가 매매 탈락

사용: python tools/mm_queue_sim.py 2026-10-01 2026-11-01 [--only 2026-10-01:KRW-WLD,KRW-ONDO 2026-11-01:KRW-WLD]
출력: data/research/queue_sim_<날짜>.json, data/research/queue_sim_summary.txt
"""
import io, json, math, os, sys, time, traceback
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mm_hist_tardis as H

LOT_KRW = 300_000
MAX_LOTS = 2
SCEN = {"보수": 0.0, "보통": None, "낙관": 1.0}
LATS = (0.15, 0.5)
OBCOLS = ["timestamp"] + [f"{s}[{i}].{k}" for i in range(5) for s in ("asks", "bids") for k in ("price", "amount")]


def load(day, sym):
    tr, c1, _ = H.fetch_csv("trades", day, sym, ["timestamp", "side", "price", "amount"])
    ob, c2, _ = H.fetch_csv("book_snapshot_5", day, sym, OBCOLS)
    if tr is None or ob is None:
        return None, None
    return tr.sort_values("timestamp", kind="stable").reset_index(drop=True), ob.sort_values("timestamp", kind="stable").reset_index(drop=True)


def size_at(levels, px):
    """levels: [(price, amount)*5] → px 가격의 물량 (없으면 0)"""
    for p, a in levels:
        if p == px:
            return a
    return 0.0


class Order:
    __slots__ = ("side", "px", "qty", "ahead", "active_at", "cancel_at", "level_seen")

    def __init__(self, side, px, qty, active_at):
        self.side, self.px, self.qty, self.ahead = side, px, qty, None
        self.active_at, self.cancel_at, self.level_seen = active_at, None, None


def simulate(tr, ob, alpha, lat, lot_qty):
    """alpha: 취소가 내 앞에서 일어나는 비율(None=비례). 돌려주는 값: dict"""
    ts_ob = ob["timestamp"].to_numpy(np.int64)
    bp = np.stack([ob[f"bids[{i}].price"].to_numpy(float) for i in range(5)], 1)
    ba = np.stack([ob[f"bids[{i}].amount"].to_numpy(float) for i in range(5)], 1)
    ap = np.stack([ob[f"asks[{i}].price"].to_numpy(float) for i in range(5)], 1)
    aa = np.stack([ob[f"asks[{i}].amount"].to_numpy(float) for i in range(5)], 1)
    ts_tr = tr["timestamp"].to_numpy(np.int64)
    tpx = tr["price"].to_numpy(float); tam = tr["amount"].to_numpy(float)
    tsell = (tr["side"].to_numpy() == "sell")            # 매도 주문이 침 → 매수 호가 쪽 체결
    L = int(lat * 1e6)
    cash = 0.0; inv = 0.0
    orders = {"buy": None, "sell": None}
    fills = []                                           # (ts, side, px, qty, mid_at_fill)
    max_inv = 0.0; n_post = 0; n_cancel = 0
    traded_at = {"buy": 0.0, "sell": 0.0}               # 마지막 호가 이후 내 가격에서 일어난 체결량
    i_ob = 0; i_tr = 0
    n_ob, n_tr = len(ts_ob), len(ts_tr)
    cur = None                                           # 현재 호가 행 번호

    def book(side, k):
        return (bp[k], ba[k]) if side == "buy" else (ap[k], aa[k])

    def best(side, k):
        return bp[k, 0] if side == "buy" else ap[k, 0]

    def better(side, a, b):                              # a 가 b 보다 좋은 가격인가(그 쪽 기준)
        return a > b if side == "buy" else a < b

    def fill(o, q, t, k):
        nonlocal cash, inv, max_inv
        q = min(q, o.qty)
        if q <= 0:
            return
        o.qty -= q
        if o.side == "buy":
            cash -= q * o.px; inv += q
        else:
            cash += q * o.px; inv -= q
        max_inv = max(max_inv, inv)
        mid = (bp[k, 0] + ap[k, 0]) / 2 if k is not None else o.px
        fills.append((t, o.side, o.px, q, mid))

    def decide(t, k):
        nonlocal n_post, n_cancel
        b1, a1 = bp[k, 0], ap[k, 0]
        if not (np.isfinite(b1) and np.isfinite(a1) and a1 > b1):
            return
        want = {"buy": inv < MAX_LOTS * lot_qty - 1e-12, "sell": inv >= lot_qty - 1e-12}
        for side in ("buy", "sell"):
            o = orders[side]
            px = best(side, k)
            if o is not None and o.cancel_at is None:
                if not want[side] or (o.qty <= 1e-12) or better(side, px, o.px):
                    o.cancel_at = t + L; n_cancel += 1
            if want[side] and (orders[side] is None or orders[side].cancel_at is not None) and pend[side] is None:
                q = lot_qty if side == "buy" else min(lot_qty, inv)
                if side == "buy":
                    q = min(q, MAX_LOTS * lot_qty - inv)
                if q > 1e-12:
                    newo = Order(side, px, q, t + L)
                    pend[side] = newo; n_post += 1

    pend = {"buy": None, "sell": None}
    t_end = int(min(ts_ob[-1], ts_tr[-1]))
    while True:
        t_ob = ts_ob[i_ob] if i_ob < n_ob else 1 << 62
        t_tr = ts_tr[i_tr] if i_tr < n_tr else 1 << 62
        # 예약된 효력(주문 활성·취소) 시각
        t_act = min([o.active_at for o in pend.values() if o is not None] + [o.cancel_at for o in orders.values() if o is not None and o.cancel_at is not None] + [1 << 62])
        t = min(t_ob, t_tr, t_act)
        if t > t_end:
            break
        if t == t_act:
            for side in ("buy", "sell"):
                o = orders[side]
                if o is not None and o.cancel_at is not None and o.cancel_at <= t:
                    orders[side] = None
                p = pend[side]
                if p is not None and p.active_at <= t and orders[side] is None:
                    if cur is not None:
                        lv = book(side, cur)
                        p.ahead = size_at(zip(*lv), p.px)
                        p.level_seen = p.ahead
                    else:
                        p.ahead = 0.0; p.level_seen = 0.0
                    if side == "sell":
                        p.qty = min(p.qty, inv)
                    orders[side] = p; pend[side] = None; traded_at[side] = 0.0
            continue
        if t == t_tr:                                    # 체결 처리 (같은 시각이면 체결을 호가보다 먼저 — 호가는 체결 뒤 상태)
            px, am, sell = tpx[i_tr], tam[i_tr], tsell[i_tr]
            side = "buy" if sell else "sell"             # 이 체결이 닿는 내 주문 쪽
            o = orders[side]
            if o is not None and o.ahead is not None and o.qty > 1e-12:
                if px == o.px:
                    traded_at[side] += am
                    use = min(am, o.ahead)
                    o.ahead -= use
                    rest = am - use
                    if rest > 0:
                        fill(o, rest, t, cur)
                elif better(side, o.px, px):              # 내 가격보다 불리한 쪽까지 쓸고 내려감 → 내 줄 소진
                    o.ahead = 0.0
                    fill(o, o.qty, t, cur)
            i_tr += 1
            continue
        # 호가 갱신
        k = i_ob
        for side in ("buy", "sell"):
            o = orders[side]
            if o is None or o.ahead is None:
                continue
            prices, amts = book(side, k)
            now_size = size_at(zip(prices, amts), o.px)
            far = prices[-1]
            in_view = (not np.isfinite(far)) or (o.px >= far if side == "buy" else o.px <= far)   # 보이는 5칸 안이거나 더 좋은 가격
            if not in_view:
                traded_at[side] = 0.0; o.level_seen = now_size; continue
            dec = (o.level_seen or 0.0) - now_size - traded_at[side]
            if dec > 0:                                  # 체결로 설명 안 되는 감소 = 취소 (늘어난 주문이 가릴 수 있어 보수적)
                if alpha is None:
                    tot = max(o.level_seen - traded_at[side], 1e-12)
                    a = min(1.0, max(0.0, o.ahead / tot))
                else:
                    a = alpha
                o.ahead = max(0.0, o.ahead - dec * a)
            if now_size <= 0:
                o.ahead = 0.0
            o.ahead = min(o.ahead, now_size)             # 앞줄이 보이는 물량보다 많을 수는 없음
            o.level_seen = now_size; traded_at[side] = 0.0
        cur = k
        decide(t, k)
        i_ob += 1

    # 하루 끝: 남은 물량 처분
    k = cur
    end_mid = (bp[k, 0] + ap[k, 0]) / 2
    left = inv; proceeds = 0.0
    for lvl in range(5):
        p, a = bp[k, lvl], ba[k, lvl]
        if not np.isfinite(p) or left <= 0:
            continue
        q = min(left, a); proceeds += q * p; left -= q
    if left > 0:
        proceeds += left * bp[k, 4] * 0.99
    liq_cost = inv * end_mid - proceeds
    pnl = cash + proceeds
    f = np.array([(x[2], x[3], x[4], 1 if x[1] == "buy" else -1) for x in fills]) if fills else np.zeros((0, 4))
    # 체결 순간 몫(중간값 대비)과 나머지(물량 평가 변화)로 나눔
    spread_part = float(((f[:, 2] - f[:, 0]) * f[:, 3] * f[:, 1]).sum()) if len(f) else 0.0
    vol_krw = float((f[:, 0] * f[:, 1]).sum()) if len(f) else 0.0
    nb = int((f[:, 3] == 1).sum()) if len(f) else 0
    ns = int((f[:, 3] == -1).sum()) if len(f) else 0
    return {"pnl_krw": round(pnl), "spread_capture_krw": round(spread_part), "inventory_and_rest_krw": round(pnl - spread_part),
            "liquidation_cost_krw": round(liq_cost), "fills_buy": nb, "fills_sell": ns, "volume_krw": round(vol_krw),
            "max_inventory_krw": round(max_inv * end_mid), "end_inventory_krw": round(inv * end_mid), "posts": n_post, "cancels": n_cancel,
            "first_fill_min": round((fills[0][0] - ts_ob[0]) / 6e7, 1) if fills else None}


def run_day(day, only=None):
    src = json.load(open(f"data/research/tardis_{day}.json"))        # E2a 와 같은 종목 목록
    syms = [x for x in src["selected"] if not only or x in only]
    R = {"day": day, "rule": src["rule"], "selected": syms, "params": {"lot_krw": LOT_KRW, "max_lots": MAX_LOTS, "lats": LATS, "scenarios": list(SCEN), "fee": 0},
         "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git": os.popen("git rev-parse HEAD").read().strip(), "symbols": {}}
    for sym in syms:
        try:
            tr, ob = load(day, sym)
            if tr is None or len(tr) < 50:
                R["symbols"][sym] = {"error": "자료 없음"}; continue
            p0 = float((ob["bids[0].price"].iloc[0] + ob["asks[0].price"].iloc[0]) / 2)
            lot_qty = LOT_KRW / p0
            res = {"price_open": p0, "day_return_pct": round((float((ob["bids[0].price"].iloc[-1] + ob["asks[0].price"].iloc[-1]) / 2) / p0 - 1) * 100, 2),
                   "e2a_front_60s_vw_bp": src["symbols"].get(sym, {}).get("markout", {}).get("front", {}).get("60s", {}).get("vw_bp"),
                   "runs": {}}
            for lat in LATS:
                for name, alpha in SCEN.items():
                    t0 = time.time()
                    r = simulate(tr, ob, alpha, lat, lot_qty)
                    r["sec"] = round(time.time() - t0, 1)
                    res["runs"][f"{name}|{lat}"] = r
            R["symbols"][sym] = res
            print(day, sym, {k: v["pnl_krw"] for k, v in res["runs"].items()}, flush=True)
        except Exception:
            R["symbols"][sym] = {"error": traceback.format_exc()[-800:]}
            print(day, sym, "ERROR", traceback.format_exc()[-300:], flush=True)
    json.dump(R, open(f"data/research/queue_sim_{day}.json", "w"), ensure_ascii=False, indent=1)
    return R


def table(Rs):
    L = ["단순 양쪽 지정가, 30만원 1개씩, 최대 보유 60만원, 수수료 0, 하루 끝 처분 포함. 단위: 원",
         f"{'날짜':6s}{'코인':7s}{'등락%':>7s}{'E2a줄앞':>8s} | " + " | ".join(f"{k:>16s}" for k in [f"{n} 지연{l}" for l in LATS for n in SCEN]) + " | 보통0.15: 매수/매도 체결, 최대보유, 처분비용, 체결순간몫"]
    for R in Rs:
        for sym, r in R["symbols"].items():
            if "error" in r:
                L.append(f"{R['day'][5:]:6s}{sym[4:]:7s} {r['error'][:60]}"); continue
            m = r["runs"]["보통|0.15"]
            L.append(f"{R['day'][5:]:6s}{sym[4:]:7s}{r['day_return_pct']:>+7.1f}{(r['e2a_front_60s_vw_bp'] or 0):>+8.2f} | " +
                     " | ".join(f"{r['runs'][f'{n}|{l}']['pnl_krw']:>16,d}" for l in LATS for n in SCEN) +
                     f" | {m['fills_buy']}/{m['fills_sell']}, {m['max_inventory_krw']:,}, {m['liquidation_cost_krw']:,}, {m['spread_capture_krw']:,}")
    return "\n".join(L)


if __name__ == "__main__":
    only = {}
    if "--only" in sys.argv:
        for a in sys.argv[sys.argv.index("--only") + 1:]:
            d, lst = a.split(":"); only[d] = lst.split(",")
    days = sorted(only) or [a for a in sys.argv[1:] if a[:2] == "20"] or ["2026-10-01", "2026-11-01"]
    Rs = [run_day(d, only.get(d)) for d in days]
    txt = table(Rs)
    open("data/research/queue_sim_summary.txt", "w").write(txt + "\n")
    print(txt)
