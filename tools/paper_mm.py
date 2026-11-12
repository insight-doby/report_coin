"""모의 마켓 메이킹 엔진 (실주문 없음) — 단순 봇 / 똑똑한 봇.

규칙은 2026-11-12 에 고정했고 2주 모의 기간 동안 바꾸지 않는다. 바꾸면 버전을 올리고 기록을 따로 센다.
RULES_VERSION = "pm1"

공통
  · 한 코인에 한 번에 주문 1개씩(매수 1, 매도 1), 크기 = 그날 첫 가격 기준 LOT_KRW
  · 보유 한도 MAX_LOTS × LOT (취소 대기 중인 매수 주문도 한도에 포함)
  · 매도는 보유가 0.2 LOT 이상이면 보유량만큼(최대 1 LOT)
  · 봇이 호가를 '보는' 시각 = 거래소 호가 시각 + 실제 수신 지연(기록에 있음, 없으면 FEED_LAT)
    주문·취소가 거래소에서 효력을 내는 시각 = 본 시각 + ORDER_LAT
  · 하루가 끝나면 남은 물량을 매수 호가 5칸을 쓸어 처분한 값으로 평가(다음 날은 0에서 다시 시작)
  · 수수료 0 (빗썸 API 무료 조건 확인 전 가정)

단순 봇: 매수는 매수 맨 앞, 매도는 매도 맨 앞. 더 좋은 가격이 맨 앞에 생기면 취소 후 다시 건다.

똑똑한 봇 (단순 봇에 세 가지 추가)
  1) 피하기: 내 앞줄이 줄을 선 때의 30% 아래로 줄었고, 최근 3초 동안 내 쪽으로 친 체결이 내 앞줄의 2배 이상이면
     → 그 주문을 취소하고 그쪽은 5초 동안 새로 걸지 않는다. (줄이 통째로 쓸리기 직전에 빠지기)
  2) 기울이기: 보유가 1 LOT 이상이면 매수는 맨 앞보다 한 칸 아래에 건다. (물량이 쌓이면 덜 사기)
  3) 쉬기: 최근 60초 중간값 변동폭이 5호가 이상이면 매수 주문을 빼고 60초 동안 새 매수를 걸지 않는다. 매도는 유지.

줄 위치: mm_queue_sim 과 같음 (보이는 물량 뒤에 선다, 체결이 앞줄부터 줄인다, 설명 안 되는 감소는 취소,
  취소가 내 앞에서 일어난 비율 alpha: 0 보수 / None 비례(보통) / 1 낙관)
"""
import numpy as np

RULES_VERSION = "pm1"
LOT_KRW = 300_000
MAX_LOTS = 2
ORDER_LAT = 0.10          # 초. 본 뒤 주문이 거래소에 닿기까지
FEED_LAT = 0.15           # 초. 수신 지연 기록이 없을 때
PULL_FRAC, PULL_FLOW_X, PULL_WINDOW, PULL_COOL = 0.30, 2.0, 3.0, 5.0
PAUSE_TICKS, PAUSE_WINDOW, PAUSE_COOL = 5, 60.0, 60.0


class Order:
    __slots__ = ("side", "px", "qty", "ahead", "ahead0", "active_at", "cancel_at", "level_seen")

    def __init__(self, side, px, qty, active_at):
        self.side, self.px, self.qty = side, px, qty
        self.ahead = self.ahead0 = self.level_seen = None
        self.active_at, self.cancel_at = active_at, None


def size_at(prices, amts, px):
    for p, a in zip(prices, amts):
        if p == px:
            return a
    return 0.0


def simulate(tr, ob, variant="단순", alpha=None, lot_qty=None, feed_lat_us=None, order_lat=ORDER_LAT, tick=None):
    """tr: timestamp(µs, 거래소 체결 시각), side(buy/sell = 치고 들어온 쪽), price, amount
    ob: timestamp(µs, 거래소 호가 시각), bids/asks[0..4].price/amount
    feed_lat_us: 호가 행마다 수신 지연(µs) 배열 또는 None
    돌려주는 값: dict (손익·체결·보유 등)"""
    smart = variant == "똑똑"
    ts_ob = ob["timestamp"].to_numpy(np.int64)
    bp = np.stack([ob[f"bids[{i}].price"].to_numpy(float) for i in range(5)], 1)
    ba = np.stack([ob[f"bids[{i}].amount"].to_numpy(float) for i in range(5)], 1)
    ap = np.stack([ob[f"asks[{i}].price"].to_numpy(float) for i in range(5)], 1)
    aa = np.stack([ob[f"asks[{i}].amount"].to_numpy(float) for i in range(5)], 1)
    ts_tr = tr["timestamp"].to_numpy(np.int64)
    tpx = tr["price"].to_numpy(float); tam = tr["amount"].to_numpy(float)
    tsell = (tr["side"].to_numpy() == "sell")
    n_ob, n_tr = len(ts_ob), len(ts_tr)
    if lot_qty is None:
        lot_qty = LOT_KRW / ((bp[0, 0] + ap[0, 0]) / 2)
    if tick is None:
        px_all = np.unique(np.concatenate([bp[:: max(1, n_ob // 500)].ravel(), ap[:: max(1, n_ob // 500)].ravel()]))
        px_all = px_all[np.isfinite(px_all)]
        d = np.diff(px_all); d = d[d > 1e-12]
        tick = float(np.round(d.min(), 10)) if d.size else 0.0
    fl = np.full(n_ob, int(FEED_LAT * 1e6), np.int64) if feed_lat_us is None else np.clip(np.asarray(feed_lat_us, np.int64), 0, 5_000_000)
    OL = int(order_lat * 1e6)

    # 봇이 호가를 '보는' 사건: 거래소 시각 + 수신 지연. 보는 순서대로 정렬
    see_t = ts_ob + fl
    see_order = np.argsort(see_t, kind="stable")
    see_t_sorted = see_t[see_order]

    cash = 0.0; inv = 0.0
    orders = {"buy": None, "sell": None}
    pend = {"buy": None, "sell": None}
    cool = {"buy": 0, "sell": 0}
    traded_at = {"buy": 0.0, "sell": 0.0}
    recent = []                                  # (t, side_hit, amount) 최근 체결 (피하기용)
    mids = []                                    # (t, mid) 최근 60초 (쉬기용)
    fills = []
    max_inv = 0.0; n_post = n_cancel = n_pull = n_pause = 0
    i_ob = i_tr = i_see = 0
    cur = None                                   # 거래소 기준 현재 호가 행

    def better(side, a, b):
        return a > b if side == "buy" else a < b

    def committed_buy():                          # 보유 + 걸려 있거나 걸 예정인 매수량
        q = inv
        for o in (orders["buy"], pend["buy"]):
            if o is not None:
                q += o.qty
        return q

    def fill(o, q, t):
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
        k = cur if cur is not None else 0
        fills.append((t, o.side, o.px, q, (bp[k, 0] + ap[k, 0]) / 2))

    def decide(t, k):
        """t: 봇이 본 시각(µs), k: 본 호가 행"""
        nonlocal n_post, n_cancel, n_pull, n_pause
        b1, a1 = bp[k, 0], ap[k, 0]
        if not (np.isfinite(b1) and np.isfinite(a1) and a1 > b1):
            return
        mid = (b1 + a1) / 2
        mids.append((t, mid))
        while mids and mids[0][0] < t - PAUSE_WINDOW * 1e6:
            mids.pop(0)
        while recent and recent[0][0] < t - PULL_WINDOW * 1e6:
            recent.pop(0)
        target = {"buy": b1, "sell": a1}
        if smart:
            lo = min(m for _, m in mids); hi = max(m for _, m in mids)
            if tick and (hi - lo) >= PAUSE_TICKS * tick and t >= cool["buy"]:
                cool["buy"] = t + int(PAUSE_COOL * 1e6); n_pause += 1
            if inv >= lot_qty - 1e-12 and tick:
                target["buy"] = b1 - tick
        want = {"buy": committed_buy() < MAX_LOTS * lot_qty - 1e-9 and t >= cool["buy"],
                "sell": inv >= 0.2 * lot_qty and t >= cool["sell"]}
        for side in ("buy", "sell"):
            o = orders[side]
            if o is not None and o.cancel_at is None:
                pull = False
                if smart and o.ahead is not None and o.ahead0 and o.qty > 1e-12:
                    hit = sum(a for tt, s, a in recent if s == side)
                    if o.ahead < PULL_FRAC * o.ahead0 and hit >= PULL_FLOW_X * max(o.ahead, 1e-12):
                        pull = True
                if pull:
                    o.cancel_at = t + OL; n_cancel += 1; n_pull += 1
                    cool[side] = t + int(PULL_COOL * 1e6)
                    want[side] = False
                elif (not want[side]) or o.qty <= 1e-12 or better(side, target[side], o.px) or (smart and side == "buy" and o.px > target["buy"]):
                    o.cancel_at = t + OL; n_cancel += 1
            if side == "buy":
                want["buy"] = want["buy"] and committed_buy() - (orders["buy"].qty if orders["buy"] is not None and orders["buy"].cancel_at is not None else 0) < MAX_LOTS * lot_qty - 1e-9
            if want[side] and (orders[side] is None or orders[side].cancel_at is not None) and pend[side] is None:
                if side == "buy":
                    q = min(lot_qty, MAX_LOTS * lot_qty - committed_buy())
                else:
                    q = min(lot_qty, inv)
                if q > 1e-9:
                    pend[side] = Order(side, target[side], q, t + OL); n_post += 1

    INF = 1 << 62
    t_end = int(min(ts_ob[-1], ts_tr[-1]))
    while True:
        t_ob = ts_ob[i_ob] if i_ob < n_ob else INF
        t_tr = ts_tr[i_tr] if i_tr < n_tr else INF
        t_see = see_t_sorted[i_see] if i_see < n_ob else INF
        acts = [o.active_at for o in pend.values() if o is not None] + \
               [o.cancel_at for o in orders.values() if o is not None and o.cancel_at is not None]
        t_act = min(acts) if acts else INF
        t = min(t_ob, t_tr, t_see, t_act)
        if t > t_end:
            break
        if t == t_act:                           # 주문·취소 효력
            for side in ("buy", "sell"):
                o = orders[side]
                if o is not None and o.cancel_at is not None and o.cancel_at <= t:
                    orders[side] = None
                p = pend[side]
                if p is not None and p.active_at <= t and orders[side] is None:
                    if side == "sell":
                        p.qty = min(p.qty, inv)
                    if p.qty > 1e-9:
                        if cur is not None:
                            prices, amts = (bp[cur], ba[cur]) if side == "buy" else (ap[cur], aa[cur])
                            p.ahead = size_at(prices, amts, p.px)
                        else:
                            p.ahead = 0.0
                        p.ahead0 = p.ahead; p.level_seen = p.ahead
                        orders[side] = p; traded_at[side] = 0.0
                    pend[side] = None
            continue
        if t == t_tr:                            # 체결 (같은 시각이면 호가보다 먼저)
            px, am, sell = tpx[i_tr], tam[i_tr], tsell[i_tr]
            side = "buy" if sell else "sell"
            recent.append((t, side, am))
            o = orders[side]
            if o is not None and o.ahead is not None and o.qty > 1e-12:
                if px == o.px:
                    traded_at[side] += am
                    use = min(am, o.ahead); o.ahead -= use
                    if am - use > 0:
                        fill(o, am - use, t)
                elif better(side, o.px, px):
                    o.ahead = 0.0; fill(o, o.qty, t)
            i_tr += 1
            continue
        if t == t_ob:                            # 거래소 호가 상태 갱신 (줄 위치 추적)
            k = i_ob
            for side in ("buy", "sell"):
                o = orders[side]
                if o is None or o.ahead is None:
                    continue
                prices, amts = (bp[k], ba[k]) if side == "buy" else (ap[k], aa[k])
                far = prices[-1]
                if np.isfinite(far) and not (o.px >= far if side == "buy" else o.px <= far):
                    traded_at[side] = 0.0; o.level_seen = 0.0; continue
                now_size = size_at(prices, amts, o.px)
                dec = (o.level_seen or 0.0) - now_size - traded_at[side]
                if dec > 0:
                    if alpha is None:
                        tot = max((o.level_seen or 0.0) - traded_at[side], 1e-12)
                        a = min(1.0, max(0.0, o.ahead / tot))
                    else:
                        a = alpha
                    o.ahead = max(0.0, o.ahead - dec * a)
                if now_size <= 0:
                    o.ahead = 0.0
                o.ahead = min(o.ahead, now_size)
                o.level_seen = now_size; traded_at[side] = 0.0
            cur = k
            i_ob += 1
            continue
        # 봇이 호가를 봄 → 판단
        k = see_order[i_see]
        decide(t, k)
        i_see += 1

    k = cur if cur is not None else n_ob - 1
    end_mid = (bp[k, 0] + ap[k, 0]) / 2
    left = inv; proceeds = 0.0
    for lvl in range(5):
        p, a = bp[k, lvl], ba[k, lvl]
        if np.isfinite(p) and left > 0:
            q = min(left, a); proceeds += q * p; left -= q
    if left > 0:
        proceeds += left * bp[k, 4] * 0.99
    pnl = cash + proceeds
    f = np.array([(x[2], x[3], x[4], 1 if x[1] == "buy" else -1) for x in fills]) if fills else np.zeros((0, 4))
    spread_part = float(((f[:, 2] - f[:, 0]) * f[:, 3] * f[:, 1]).sum()) if len(f) else 0.0
    return {"pnl_krw": int(round(pnl)), "spread_capture_krw": int(round(spread_part)), "inventory_and_rest_krw": int(round(pnl - spread_part)),
            "liquidation_cost_krw": int(round(inv * end_mid - proceeds)), "fills_buy": int((f[:, 3] == 1).sum()) if len(f) else 0,
            "fills_sell": int((f[:, 3] == -1).sum()) if len(f) else 0, "volume_krw": int(round(float((f[:, 0] * f[:, 1]).sum()))) if len(f) else 0,
            "max_inventory_krw": int(round(max_inv * end_mid)), "end_inventory_krw": int(round(inv * end_mid)),
            "posts": n_post, "cancels": n_cancel, "pulls": n_pull, "pauses": n_pause, "tick": tick, "lot_qty": lot_qty}
