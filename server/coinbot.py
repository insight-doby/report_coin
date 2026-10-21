#!/usr/bin/env python3
"""
빗썸 계좌 관리 봇 — 개인 서버(고정 IP)에서 실행합니다.

하는 일
  · 2분마다   체결 확인 → 매수/매도 체결 알림 (매도는 실현 손익까지)
  · 10분마다  계좌 스냅샷(총자산·보유·현금) + 입출금 확인
  · 매일 22시 하루 결산 / 매월 1일 09시 지난달 성적표
  · 텔레그램 메뉴: /account /holdings /perf /trades /orders /goal

보안
  · 빗썸 키는 '조회' 권한만 사용합니다 (주문·출금 기능 없음).
  · 금액·수량은 텔레그램 1:1 대화로만 보내고, 로그에는 남기지 않습니다.
  · 공개 저장소에는 (선택) 종목·평단·비중만 올립니다.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import traceback
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

KST = timezone(timedelta(hours=9))
API = "https://api.bithumb.com"
AK = os.environ.get("BITHUMB_ACCESS_KEY", "").strip()
SK = os.environ.get("BITHUMB_SECRET_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_REPO", "insight-doby/report_coin").strip()
DB_PATH = os.environ.get("COINBOT_DB", "/var/lib/coinbot/coinbot.db")
GOAL_PCT = float(os.environ.get("COINBOT_GOAL_PCT", "30"))     # 연 목표 수익률(%)
REPORT_HOUR = int(os.environ.get("COINBOT_REPORT_HOUR", "22"))  # 하루 결산 시각(KST)

FILL_EVERY = 120
SNAP_EVERY = 600
DUST_KRW = 5_000               # 평가금액 이보다 작으면 '보유'로 보지 않음
PUBLIC_MIN_KRW = 10_000        # 공개 보유 목록에 올릴 최소 금액
DONE_STATES = {"ACCEPTED", "DONE", "COMPLETE", "COMPLETED", "SUCCESS", "TRAVEL_RULE_ACCEPTED"}
FW = "　"                  # 전각 공백 (한글 라벨 줄맞춤용)


def log(msg: str):
    print(f"{datetime.now(KST):%m-%d %H:%M:%S} {msg}", flush=True)


def now_ts() -> float:
    return time.time()


def fnum(v, d=0.0) -> float:
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def parse_ts(s) -> float:
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.timestamp()
    except ValueError:
        return 0.0


def kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, KST)


# ───────────────────────── 숫자 표시 ─────────────────────────
def won(v) -> str:
    return f"{fnum(v):,.0f}원"


def man(v, sign=False) -> str:
    """금액을 짧게: 1,234,567 → 123.5만 / 123,456,789 → 1.23억 / 9,800 → 9,800"""
    v = fnum(v)
    s = "+" if sign and v > 0 else ("-" if v < 0 else "")
    a = abs(v)
    if a >= 1e8:
        body = f"{a / 1e8:.2f}억"
    elif a >= 1e5:
        body = f"{a / 1e4:,.1f}만"
    else:
        body = f"{a:,.0f}"
    return s + body


def pct(v, digits=1) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%"


def price_txt(v) -> str:
    v = fnum(v)
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def rp(v: float) -> float:
    v = float(v)
    if v >= 1000:
        return float(int(round(v)))
    if v >= 100:
        return round(v, 1)
    if v >= 1:
        return round(v, 2)
    if v >= 0.01:
        return round(v, 4)
    return round(v, 8)


def esc(t: str) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def label(t: str, width=3) -> str:
    """한글 라벨을 전각 공백으로 같은 폭으로 맞춤."""
    return t + FW * max(0, width - len(t))


def dot(v) -> str:
    if v is None:
        return "⚪"
    return "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")


# ───────────────────────── 빗썸 API ─────────────────────────
class BithumbError(Exception):
    def __init__(self, status, name=""):
        super().__init__(f"HTTP {status} {name}".strip())
        self.status, self.name = status, name


def _pairs(params):
    out = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            out += [(f"{k}[]", str(x)) for x in v]
        else:
            out.append((k, str(v)))
    return out


def _jwt(params=None) -> str:
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    payload = {"access_key": AK, "nonce": str(uuidlib.uuid4()), "timestamp": int(time.time() * 1000)}
    pairs = _pairs(params)
    if pairs:
        raw = "&".join(f"{k}={v}" for k, v in pairs)
        payload["query_hash"] = hashlib.sha512(raw.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64(hmac.new(SK.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def bget(path: str, params=None, private=True):
    pairs = _pairs(params)
    qs = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)
    url = API + path + (f"?{qs}" if qs else "")
    headers = {"Accept": "application/json"}
    if private:
        headers["Authorization"] = f"Bearer {_jwt(params)}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        name = ""
        try:
            name = str((r.json().get("error") or {}).get("name", ""))[:60]
        except Exception:
            pass
        raise BithumbError(r.status_code, name)        # 응답 본문(금액 포함 가능)은 남기지 않음
    return r.json()


def fetch_prices(coins) -> dict:
    coins = sorted({c for c in coins if c and c != "KRW"})
    out = {}
    for i in range(0, len(coins), 40):
        chunk = coins[i:i + 40]
        try:
            data = bget("/v1/ticker", {"markets": ",".join(f"KRW-{c}" for c in chunk)}, private=False)
        except BithumbError:
            data = []
            for c in chunk:                      # 상장폐지 종목 하나 때문에 묶음 전체가 실패하는 경우
                try:
                    data += bget("/v1/ticker", {"markets": f"KRW-{c}"}, private=False)
                except BithumbError:
                    pass
        for t in data or []:
            out[str(t.get("market", "")).split("-")[-1]] = fnum(t.get("trade_price"))
    return out


# ───────────────────────── 저장소 ─────────────────────────
class DB:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS snaps(ts INTEGER PRIMARY KEY, total REAL, cash REAL);
        CREATE TABLE IF NOT EXISTS flows(id TEXT PRIMARY KEY, ts INTEGER, kind TEXT, coin TEXT, qty REAL, krw REAL);
        CREATE TABLE IF NOT EXISTS orders(uuid TEXT PRIMARY KEY, ts INTEGER, coin TEXT, side TEXT,
                                          volume REAL, funds REAL, fee REAL);
        CREATE TABLE IF NOT EXISTS trades(uuid TEXT PRIMARY KEY, ts INTEGER, coin TEXT, qty REAL, avg REAL,
                                          px REAL, pnl REAL, pct REAL, fee REAL, hold_h REAL);
        CREATE TABLE IF NOT EXISTS pos(coin TEXT PRIMARY KEY, qty REAL, cost REAL, opened INTEGER);
        """)
        self.c.commit()

    def get(self, k, d=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else d

    def put(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        self.c.commit()

    def q(self, sql, args=()):
        return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        self.c.execute(sql, args)
        self.c.commit()


# ───────────────────────── 텔레그램 ─────────────────────────
def tg(method, **params):
    if not TG_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params,
                          timeout=params.get("timeout", 10) + 10)
        return r.json()
    except Exception as e:
        log(f"텔레그램 오류: {type(e).__name__}")
        return None


def send(text: str):
    if not (TG_TOKEN and TG_CHAT):
        print(text)
        return
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=TG_CHAT, text=text[i:i + 3900], parse_mode="HTML",
           disable_web_page_preview=True)


COMMANDS = [
    ("account", "💼 계좌 한눈에 (총자산·손익·목표)"),
    ("holdings", "📦 보유 코인별 수익률"),
    ("perf", "📈 매매 성적 (승률·손익비·낙폭)"),
    ("trades", "🧾 최근 매도 기록"),
    ("orders", "⏳ 걸어둔 주문"),
    ("goal", "🎯 연 30% 목표 진행"),
    ("help", "❓ 사용법"),
]
WORDS = {
    "account": ["계좌", "자산", "잔고", "전체"], "holdings": ["보유", "코인", "종목"],
    "perf": ["성적", "수익", "승률", "성과"], "trades": ["거래", "매도", "기록", "체결"],
    "orders": ["주문", "지정가", "대기"], "goal": ["목표"], "help": ["도움", "help", "start", "메뉴"],
}


# ───────────────────────── 계좌 관리 ─────────────────────────
class Bot:
    def __init__(self, db: DB):
        self.db = db
        self.last_err_alert = 0.0
        self.err_count = 0

    # ── 계좌·시세 ──
    def accounts(self):
        acc = bget("/v1/accounts")
        cash, hold = 0.0, {}
        for a in acc:
            cur = str(a.get("currency", "")).upper()
            qty = fnum(a.get("balance")) + fnum(a.get("locked"))
            if cur == "KRW":
                cash = qty
            elif qty > 0:
                hold[cur] = {"qty": qty, "avg": fnum(a.get("avg_buy_price"))}
        return cash, hold

    def state(self):
        """현재 계좌 상태(시세 반영). 금액은 메모리/DB에만."""
        cash, hold = self.accounts()
        prices = fetch_prices(hold.keys())
        rows = {}
        for c, h in hold.items():
            p = prices.get(c)
            if not p:
                continue
            v = h["qty"] * p
            if v < DUST_KRW:
                continue
            cost = h["qty"] * h["avg"]
            rows[c] = {**h, "price": p, "value": v, "pnl": v - cost if h["avg"] else None,
                       "pct": (p / h["avg"] - 1) * 100 if h["avg"] else None}
        total = cash + sum(r["value"] for r in rows.values())
        for r in rows.values():
            r["weight"] = r["value"] / total * 100 if total else 0
        self.remember_avg(hold)
        return {"cash": cash, "hold": rows, "total": total, "ts": now_ts()}

    def remember_avg(self, hold):
        """빗썸이 계산한 평단을 기억 (전량 매도 후에도 직전 평단을 쓰도록 병합 저장)."""
        avg = self.db.get("acct_avg", {}) or {}
        avg.update({c: h["avg"] for c, h in hold.items() if h["avg"]})
        self.db.put("acct_avg", avg)

    # ── 체결 ──
    def _orders(self, params, pages=1):
        try:
            return self._orders_raw(params, pages)
        except BithumbError as e:
            if e.status != 400 or "market" in params:
                raise
        out = []                                  # 마켓 지정이 필요한 경우: 아는 코인별로 조회
        coins = set((self.db.get("acct_avg", {}) or {}).keys()) | {r[0] for r in self.db.q("SELECT coin FROM pos")}
        for c in sorted(coins):
            try:
                out += self._orders_raw({**params, "market": f"KRW-{c}"}, pages)
            except BithumbError:
                pass
        return out

    def _orders_raw(self, params, pages):
        out = []
        for page in range(1, pages + 1):
            data = bget("/v1/orders", {**params, "page": page, "limit": 100, "order_by": "desc"})
            out += data or []
            if not data or len(data) < 100:
                break
        return out

    def _detail(self, u):
        o = bget("/v1/order", {"uuid": u})
        trades = o.get("trades") or []
        vol = fnum(o.get("executed_volume")) or sum(fnum(t.get("volume")) for t in trades)
        funds = sum(fnum(t.get("funds")) or fnum(t.get("price")) * fnum(t.get("volume")) for t in trades)
        if not funds and vol:
            funds = vol * fnum(o.get("price"))
        ts = max([parse_ts(t.get("created_at")) for t in trades] or [0]) or parse_ts(o.get("created_at"))
        mk = str(o.get("market", ""))
        return {"uuid": o.get("uuid", u), "market": mk, "coin": mk.split("-")[-1], "side": o.get("side"),
                "state": o.get("state"), "volume": vol, "funds": funds, "fee": fnum(o.get("paid_fee")), "ts": ts}

    def sync_fills(self, notify=True, backfill=False):
        if not backfill:
            try:
                self.remember_avg(self.accounts()[1])
            except BithumbError:
                pass
        known = set(r[0] for r in self.db.q("SELECT uuid FROM orders"))
        prev_open = set(self.db.get("open_orders", []))
        open_now = self._orders({"state": "wait"}, pages=3)
        self.db.put("open_orders", [o.get("uuid") for o in open_now])
        self.db.put("open_orders_full", [
            {"coin": str(o.get("market", "")).split("-")[-1], "side": o.get("side"), "price": fnum(o.get("price")),
             "vol": fnum(o.get("remaining_volume") or o.get("volume")), "ts": parse_ts(o.get("created_at"))}
            for o in open_now])
        cand = prev_open - {o.get("uuid") for o in open_now}
        recent = self._orders({"states": ["done", "cancel"]}, pages=20 if backfill else 1)
        for o in recent:
            if fnum(o.get("executed_volume")) > 0:
                cand.add(o.get("uuid"))
        cand -= known
        fills = []
        for u in cand:
            if not u:
                continue
            try:
                d = self._detail(u)
            except BithumbError as e:
                log(f"주문 상세 실패: {e}")
                continue
            if d["volume"] > 0 and d["market"].startswith("KRW-") and d["state"] != "wait":
                fills.append(d)
        fills.sort(key=lambda d: d["ts"])
        for d in fills:
            self.apply_fill(d, notify=notify and not backfill, prefer_replay=backfill)
        if fills:
            log(f"체결 {len(fills)}건 반영")
        return fills

    def _pos(self, coin):
        r = self.db.q("SELECT qty,cost,opened FROM pos WHERE coin=?", (coin,))
        return dict(r[0]) if r else {"qty": 0.0, "cost": 0.0, "opened": None}

    def _save_pos(self, coin, p):
        if p["qty"] <= 1e-12:
            self.db.x("DELETE FROM pos WHERE coin=?", (coin,))
        else:
            self.db.x("INSERT OR REPLACE INTO pos VALUES(?,?,?,?)", (coin, p["qty"], p["cost"], p["opened"]))

    def apply_fill(self, d, notify=True, prefer_replay=False):
        coin, vol, funds, fee, ts = d["coin"], d["volume"], d["funds"], d["fee"], d["ts"]
        px = funds / vol if vol else 0
        p = self._pos(coin)
        msg = None
        if d["side"] == "bid":
            if p["qty"] <= 1e-12:
                p["opened"] = int(ts)
            old_avg = p["cost"] / p["qty"] if p["qty"] > 0 else None
            p["qty"] += vol
            p["cost"] += funds + fee
            new_avg = p["cost"] / p["qty"]
            avg_txt = (f"평단 {price_txt(old_avg)} → <b>{price_txt(new_avg)}</b>" if old_avg
                       else f"평단 <b>{price_txt(new_avg)}</b> (새로 매수)")
            msg = (f"🟢 <b>매수 체결 · {coin}</b>\n{price_txt(px)}원 × {vol:,.4g}개 = {won(funds)}\n{avg_txt}")
        else:
            proceeds = funds - fee
            replay_avg = p["cost"] / p["qty"] if p["qty"] >= vol * 0.98 and p["qty"] > 0 else None
            acct_avg = (self.db.get("acct_avg", {}) or {}).get(coin)
            avg = (replay_avg or acct_avg) if prefer_replay else (acct_avg or replay_avg)
            pnl = proceeds - avg * vol if avg else None
            pc = pnl / (avg * vol) * 100 if avg else None
            hold_h = (ts - p["opened"]) / 3600 if p.get("opened") else None
            self.db.x("INSERT OR REPLACE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (d["uuid"], int(ts), coin, vol, avg, px, pnl, pc, fee, hold_h))
            take = min(vol, p["qty"])
            if p["qty"] > 0:
                p["cost"] -= p["cost"] / p["qty"] * take
                p["qty"] -= take
            if p["qty"] * px < DUST_KRW:
                p = {"qty": 0.0, "cost": 0.0, "opened": None}
            left = "전량 매도" if p["qty"] <= 1e-12 else f"남은 {p['qty']:,.4g}개"
            if pnl is not None:
                msg = (f"{'🔵' if pnl >= 0 else '🔴'} <b>매도 체결 · {coin}</b> ({left})\n"
                       f"{price_txt(px)}원 × {vol:,.4g}개 = {won(funds)}\n"
                       f"수익 <b>{pct(pc, 2)}</b> · {man(pnl, True)}원 (수수료 포함)"
                       + (f" · {hold_h / 24:.1f}일 보유" if hold_h else ""))
            else:
                msg = f"🔵 <b>매도 체결 · {coin}</b> ({left})\n{price_txt(px)}원 × {vol:,.4g}개 = {won(funds)}"
        self._save_pos(coin, p)
        self.db.x("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?)",
                  (d["uuid"], int(ts), coin, d["side"], vol, funds, fee))
        if notify and msg:
            send(msg)

    def reconcile(self, st):
        """체결 기록으로 만든 보유를 실제 계좌(빗썸 평단)에 맞춤 — 외부 입출금·기록 누락 보정."""
        held = st["hold"]
        for r in self.db.q("SELECT coin FROM pos"):
            if r[0] not in held:
                self.db.x("DELETE FROM pos WHERE coin=?", (r[0],))
        for c, h in held.items():
            p = self._pos(c)
            if p["qty"] <= 0 or abs(p["qty"] - h["qty"]) / h["qty"] > 0.01:
                p["qty"] = h["qty"]
                p["cost"] = h["qty"] * (h["avg"] or h["price"])
                p["opened"] = p.get("opened") or int(st["ts"])
                self._save_pos(c, p)

    # ── 입출금 ──
    def sync_flows(self, prices, notify=True):
        start = self.db.get("start_ts")
        missing = []
        new = []
        for path, kind, sign in (("/v1/deposits/krw", "입금", 1), ("/v1/withdraws/krw", "출금", -1),
                                 ("/v1/deposits", "코인 입금", 1), ("/v1/withdraws", "코인 출금", -1)):
            try:
                items = bget(path, {"limit": 100, "order_by": "desc"}) or []
            except BithumbError as e:
                if e.status in (401, 403):
                    missing.append(kind)
                continue
            for it in items:
                if str(it.get("state", "")).upper() not in DONE_STATES:
                    continue
                ts = parse_ts(it.get("done_at") or it.get("created_at"))
                fid = f"{path}:{it.get('uuid') or it.get('txid')}"
                if start is None or ts < start or self.db.q("SELECT 1 FROM flows WHERE id=?", (fid,)):
                    continue
                coin = str(it.get("currency", "KRW")).upper()
                qty = fnum(it.get("amount"))
                if coin == "KRW":
                    krw = qty
                else:
                    p = prices.get(coin) or fetch_prices([coin]).get(coin, 0)
                    krw = qty * p
                self.db.x("INSERT INTO flows VALUES(?,?,?,?,?,?)", (fid, int(ts), kind, coin, qty, sign * krw))
                new.append((kind, coin, qty, krw))
        self.db.put("flow_perm_missing", missing)
        for kind, coin, qty, krw in new:
            log(f"{kind} 확인 1건")
            if notify:
                what = won(krw) if coin == "KRW" else f"{coin} {qty:,.4g}개 (약 {won(krw)})"
                send(f"💰 <b>{kind} 확인</b> · {what}\n수익률 계산에서는 빼고 계산합니다.")

    # ── 스냅샷 ──
    def snapshot(self, notify=True):
        st = self.state()
        if self.db.get("start_ts") is None:
            self.db.put("start_ts", int(st["ts"]))
        self.sync_flows({c: h["price"] for c, h in st["hold"].items()}, notify=notify)
        self.db.x("INSERT OR REPLACE INTO snaps VALUES(?,?,?)", (int(st["ts"]), st["total"], st["cash"]))
        self.reconcile(st)
        self.db.put("last_state", st)
        log(f"스냅샷: 보유 {len(st['hold'])}종목")
        if GH_TOKEN:
            try:
                self.publish_public(st)
            except Exception as e:
                log(f"공개 보유목록 갱신 실패: {type(e).__name__}")
        return st

    # ── 수익률 계산 (입출금 제외, 시간가중) ──
    def curve(self):
        snaps = [(r["ts"], r["total"]) for r in self.db.q("SELECT ts,total FROM snaps ORDER BY ts")]
        flows = [(r["ts"], r["krw"]) for r in self.db.q("SELECT ts,krw FROM flows ORDER BY ts")]
        idx, out, fi, prev = 1.0, [], 0, None
        for ts, tot in snaps:
            f = 0.0
            while fi < len(flows) and flows[fi][0] <= ts:
                if prev is not None:
                    f += flows[fi][1]
                fi += 1
            if prev is not None and prev[1] > 0:
                idx *= (tot - f) / prev[1]
            out.append((ts, tot, idx))
            prev = (ts, tot)
        return out, flows

    def period(self, t_from, curve=None, flows=None):
        if curve is None:
            curve, flows = self.curve()
        if not curve:
            return None
        base = None
        for row in curve:
            if row[0] <= t_from:
                base = row
            else:
                break
        base = base or curve[0]
        end = curve[-1]
        fsum = sum(k for ts, k in flows if base[0] < ts <= end[0])
        return {"pnl": end[1] - base[1] - fsum, "ret": (end[2] / base[2] - 1) * 100 if base[2] else 0,
                "from": base[0]}

    def drawdown(self, curve):
        peak, mdd = 0.0, 0.0
        for _, _, i in curve:
            peak = max(peak, i)
            mdd = min(mdd, i / peak - 1)
        cur = curve[-1][2] / peak - 1 if curve and peak else 0
        return mdd * 100, cur * 100

    def stats(self, t_from=0):
        rows = self.db.q("SELECT * FROM trades WHERE ts>=? AND pct IS NOT NULL ORDER BY ts", (t_from,))
        n = len(rows)
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        gw = sum(r["pnl"] for r in wins)
        gl = -sum(r["pnl"] for r in losses)
        holds = [r["hold_h"] for r in rows if r["hold_h"]]
        fee = sum(r[0] or 0 for r in self.db.q("SELECT fee FROM orders WHERE ts>=?", (t_from,)))
        return {
            "n": n, "w": len(wins), "l": len(losses), "wr": len(wins) / n * 100 if n else None,
            "pnl": gw - gl, "avg_w": sum(r["pct"] for r in wins) / len(wins) if wins else None,
            "avg_l": sum(r["pct"] for r in losses) / len(losses) if losses else None,
            "pf": gw / gl if gl else (None if not gw else float("inf")),
            "exp": sum(r["pct"] for r in rows) / n if n else None,
            "hold_d": sum(holds) / len(holds) / 24 if holds else None, "fee": fee,
            "best": max(rows, key=lambda r: r["pct"]) if rows else None,
            "worst": min(rows, key=lambda r: r["pct"]) if rows else None,
        }

    def bounds(self, now=None):
        d = kst(now or now_ts())
        day = d.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"day": day.timestamp(), "week": (day - timedelta(days=d.weekday())).timestamp(),
                "month": day.replace(day=1).timestamp(), "year": day.replace(month=1, day=1).timestamp()}

    def goal(self, curve, flows):
        """목표 기간: 기록 시작 후 첫 1년은 '시작일~1년 뒤', 그 다음부터는 달력 연도."""
        if not curve:
            return None
        b = self.bounds()
        first, now = curve[0][0], now_ts()
        if now - first < 365 * 86400:
            start, end = first, first + 365 * 86400
        else:
            start = max(first, b["year"])
            end = kst(now).replace(month=12, day=31, hour=23, minute=59).timestamp()
        y = self.period(start, curve, flows)
        days = max((curve[-1][0] - start) / 86400, 0.0)
        target = ((1 + GOAL_PCT / 100) ** (days / 365) - 1) * 100
        months_left = max((end - now) / 86400 / 30.44, 0.25)
        need_total = (1 + GOAL_PCT / 100) / (1 + y["ret"] / 100)
        need_m = (need_total ** (1 / months_left) - 1) * 100 if need_total > 0 else None
        month_goal = ((1 + GOAL_PCT / 100) ** (1 / 12) - 1) * 100
        m = self.period(b["month"], curve, flows)
        return {"ret": y["ret"], "pnl": y["pnl"], "target": target, "gap": y["ret"] - target, "days": days,
                "need_m": need_m, "month": m["ret"], "month_goal": month_goal, "since": start, "end": end}

    # ── 체크 포인트 (돈 버는 습관) ──
    def checks(self, st, s_all, g):
        tips = []
        total = st["total"] or 1
        cash_w = st["cash"] / total * 100
        if cash_w < 10:
            tips.append(f"현금이 {cash_w:.0f}%뿐이에요 — 급락 때 주울 돈(10~30%)을 남겨두세요")
        elif cash_w > 80 and st["hold"]:
            tips.append(f"현금 {cash_w:.0f}% — 기다리는 중이면 OK, 모으기 구간 알림을 활용하세요")
        for c, h in sorted(st["hold"].items(), key=lambda x: -x[1]["weight"]):
            if h["weight"] > 40:
                tips.append(f"{c} 비중 {h['weight']:.0f}% — 한 종목 40% 넘으면 그 코인이 곧 내 계좌예요")
            if h["pct"] is not None and h["pct"] <= -10:
                tips.append(f"{c} {h['pct']:.1f}% — 손절 기준 다시 확인 (물타기는 계획 안에서만)")
        for c, stop in (self.public_stops() or {}).items():
            h = st["hold"].get(c)
            if h and h["price"] < stop:
                tips.append(f"{c} 현재가가 손절가 {price_txt(stop)} 아래예요")
        if s_all["n"] >= 5 and s_all["avg_w"] is not None and s_all["avg_l"] is not None \
                and abs(s_all["avg_l"]) > s_all["avg_w"] and (s_all["wr"] or 0) < 65:
            tips.append(f"평균 손실({s_all['avg_l']:.1f}%)이 평균 이익({s_all['avg_w']:+.1f}%)보다 커요 — 손절은 빨리, 익절은 조금 길게")
        if s_all["n"] >= 5 and s_all["pnl"] > 0 and s_all["fee"] > s_all["pnl"] * 0.25:
            tips.append(f"수수료가 실현수익의 {s_all['fee'] / s_all['pnl'] * 100:.0f}% — 잦은 매매를 줄이면 그대로 수익")
        if g and g["days"] >= 14 and g["gap"] < -5:
            tips.append(f"목표보다 {abs(g['gap']):.1f}%p 뒤처짐 — 무리한 추격보다 승률 높은 자리만")
        return tips

    def public_stops(self):
        cache = self.db.get("pub_stops_cache")
        if cache and now_ts() - cache.get("t", 0) < 1800:
            return cache["v"]
        try:
            r = requests.get(f"https://raw.githubusercontent.com/{GH_REPO}/main/data/positions.json", timeout=10)
            v = {p["ticker"]: fnum(p["stop"]) for p in r.json().get("positions", []) if p.get("stop")}
        except Exception:
            v = (cache or {}).get("v", {})
        self.db.put("pub_stops_cache", {"t": now_ts(), "v": v})
        return v

    # ── 메시지 ──
    def head(self, st, title):
        t = kst(st["ts"])
        wd = "월화수목금토일"[t.weekday()]
        cash_w = st["cash"] / st["total"] * 100 if st["total"] else 0
        return (f"💼 <b>{title}</b> · {t:%m/%d}({wd}) {t:%H:%M}\n"
                f"총자산 <b>{won(st['total'])}</b>\n현금 {man(st['cash'])}원 ({cash_w:.0f}%) · 코인 {len(st['hold'])}종목")

    def block_periods(self, curve, flows):
        b = self.bounds()
        rows = []
        for name, key in (("오늘", "day"), ("이번주", "week"), ("이번달", "month"), ("올해", "year")):
            p = self.period(b[key], curve, flows)
            if p:
                rows.append(f"{label(name)} {man(p['pnl'], True):>9} {pct(p['ret']):>7}")
        start = self.db.get("start_ts")
        note = f"\n<i>{kst(start):%m/%d} 기록 시작 · 입출금 제외</i>" if start else ""
        return "📅 <b>기간별 손익</b>\n<pre>" + esc("\n".join(rows)) + "</pre>" + note

    def block_holdings(self, st):
        if not st["hold"]:
            return "📦 <b>보유 코인</b>\n없음 (전부 현금)"
        rows = []
        upnl = 0.0
        for c, h in sorted(st["hold"].items(), key=lambda x: -x[1]["value"]):
            upnl += h["pnl"] or 0
            rows.append(f"{dot(h['pct'])}{c:<6}{h['weight']:>4.0f}% {pct(h['pct']):>7} {man(h['pnl'], True):>8}")
        inv = sum(h["qty"] * h["avg"] for h in st["hold"].values() if h["avg"])
        up = upnl / inv * 100 if inv else None
        return ("📦 <b>보유 코인</b>  <i>비중 · 수익률 · 평가손익</i>\n<pre>" + esc("\n".join(rows)) + "</pre>"
                f"\n평가손익 합계 <b>{man(upnl, True)}원</b> ({pct(up)})")

    def block_holdings_detail(self, st):
        lines = []
        for c, h in sorted(st["hold"].items(), key=lambda x: -x[1]["value"]):
            lines.append(f"{dot(h['pct'])} <b>{c}</b> {pct(h['pct'])} · 비중 {h['weight']:.0f}%\n"
                         f"   평단 {price_txt(h['avg'])} → 현재 {price_txt(h['price'])}\n"
                         f"   {h['qty']:,.4g}개 · 평가 {won(h['value'])} ({man(h['pnl'], True)})")
        return "\n".join(lines) or "보유 코인 없음"

    def block_goal(self, g):
        if not g:
            return ""
        state = "🟢 앞서는 중" if g["gap"] >= 0 else "🔴 뒤처짐"
        lines = [f"🎯 <b>연 {GOAL_PCT:.0f}% 목표</b> ({kst(g['since']):%m/%d}부터 {g['days']:.0f}일째)",
                 f"지금까지 <b>{pct(g['ret'])}</b> ({man(g['pnl'], True)}원) · 목표 페이스 {pct(g['target'])}",
                 f"{state} {abs(g['gap']):.1f}%p",
                 f"이번달 {pct(g['month'])} / 월 목표 {pct(g['month_goal'])}"]
        if g["need_m"] is not None:
            lines.append(f"{kst(g['end']):%Y/%m/%d}까지 {GOAL_PCT:.0f}% 채우려면 남은 기간 매달 {pct(g['need_m'])}")
        return "\n".join(lines)

    def block_perf(self, curve):
        b = self.bounds()
        sm, sa = self.stats(b["month"]), self.stats(0)
        mdd, cdd = self.drawdown(curve) if curve else (0, 0)

        def line(name, s):
            if not s["n"]:
                return f"{name}: 매도 기록 없음"
            return f"{name}: {s['n']}건 · 승률 {s['wr']:.0f}% ({s['w']}승 {s['l']}패) · {man(s['pnl'], True)}원"

        out = ["📈 <b>매매 성적</b> (매도 체결 기준)", line("이번달", sm), line("누적", sa)]
        if sa["n"]:
            pf = "∞" if sa["pf"] == float("inf") else (f"{sa['pf']:.2f}" if sa["pf"] is not None else "-")
            out.append(f"평균 이익 {pct(sa['avg_w'])} / 평균 손실 {pct(sa['avg_l'])}")
            out.append(f"손익비(PF) {pf} · 거래당 기대값 {pct(sa['exp'], 2)}")
            if sa["hold_d"]:
                out.append(f"평균 보유 {sa['hold_d']:.1f}일")
        out.append(f"수수료 누적 {won(sa['fee'])}")
        out.append(f"최대 낙폭 {mdd:.1f}% · 지금 고점 대비 {cdd:.1f}%")
        return "\n".join(out), sa

    def block_trades(self, limit=10):
        rows = self.db.q("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
        if not rows:
            return "🧾 <b>최근 매도</b>\n아직 기록이 없어요"
        lines = []
        for r in rows:
            hd = f"{r['hold_h'] / 24:.1f}d" if r["hold_h"] else "  - "
            lines.append(f"{kst(r['ts']):%m/%d} {r['coin']:<6}{hd:>5} {pct(r['pct']):>7} {man(r['pnl'], True) if r['pnl'] is not None else '-':>8}")
        return "🧾 <b>최근 매도</b>  <i>날짜 · 코인 · 보유일 · 수익률 · 손익</i>\n<pre>" + esc("\n".join(lines)) + "</pre>"

    def block_orders(self):
        rows = self.db.get("open_orders_full", []) or []
        if not rows:
            return "⏳ <b>걸어둔 주문</b>\n없음"
        prices = fetch_prices({r["coin"] for r in rows})
        lines = []
        for r in sorted(rows, key=lambda r: (r["coin"], r["side"])):
            cur = prices.get(r["coin"])
            dist = (r["price"] / cur - 1) * 100 if cur else None
            side = "매수" if r["side"] == "bid" else "매도"
            lines.append(f"{'🟢' if r['side'] == 'bid' else '🔵'} {r['coin']} {side} {price_txt(r['price'])}원 × {r['vol']:,.4g}"
                         f" ({won(r['price'] * r['vol'])})" + (f" · 현재가 대비 {pct(dist)}" if dist is not None else ""))
        return "⏳ <b>걸어둔 주문</b>\n" + "\n".join(lines)

    def full_report(self, st=None, title="내 계좌"):
        st = st or self.db.get("last_state") or self.snapshot()
        curve, flows = self.curve()
        g = self.goal(curve, flows)
        perf, sa = self.block_perf(curve)
        parts = [self.head(st, title), self.block_periods(curve, flows), self.block_holdings(st),
                 self.block_goal(g), perf]
        tips = self.checks(st, sa, g)
        if tips:
            parts.append("💡 <b>체크 포인트</b>\n" + "\n".join(f"• {esc(t)}" for t in tips))
        missing = self.db.get("flow_perm_missing", [])
        if missing:
            parts.append(f"<i>⚠️ {', '.join(missing)} 조회 권한이 없어 입출금이 수익으로 잡힐 수 있어요 (빗썸 API 키에 입금조회·출금조회 추가)</i>")
        return "\n\n".join(p for p in parts if p)

    def month_report(self):
        now = kst(now_ts())
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev = (first - timedelta(days=1)).replace(day=1)
        curve, flows = self.curve()
        pts = [r for r in curve if prev.timestamp() <= r[0] <= first.timestamp()]
        if len(pts) < 2:
            return None
        end_idx = pts[-1][2]
        start_row = next((r for r in reversed(curve) if r[0] <= prev.timestamp()), curve[0])
        ret = (end_idx / start_row[2] - 1) * 100
        fsum = sum(k for ts, k in flows if start_row[0] < ts <= pts[-1][0])
        pnl = pts[-1][1] - start_row[1] - fsum
        s_rows = [r for r in self.db.q("SELECT * FROM trades WHERE ts>=? AND ts<? AND pct IS NOT NULL",
                                       (prev.timestamp(), first.timestamp()))]
        mg = ((1 + GOAL_PCT / 100) ** (1 / 12) - 1) * 100
        lines = [f"🗓 <b>{prev:%Y년 %m월} 성적표</b>",
                 f"수익률 <b>{pct(ret)}</b> ({man(pnl, True)}원) · 월 목표 {pct(mg)} → {'✅ 달성' if ret >= mg else '❌ 미달'}",
                 f"매도 {len(s_rows)}건"]
        if s_rows:
            w = [r for r in s_rows if r["pnl"] > 0]
            best = max(s_rows, key=lambda r: r["pct"])
            worst = min(s_rows, key=lambda r: r["pct"])
            lines[-1] += f" · 승률 {len(w) / len(s_rows) * 100:.0f}%"
            lines.append(f"최고 {best['coin']} {pct(best['pct'])} / 최저 {worst['coin']} {pct(worst['pct'])}")
        return "\n".join(lines)

    # ── 공개 보유 목록 (종목·평단·비중만) ──
    def publish_public(self, st):
        url = f"https://api.github.com/repos/{GH_REPO}/contents/data/positions.json"
        h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=h, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"GET {r.status_code}")
        meta = r.json()
        cur = json.loads(base64.b64decode(meta["content"]).decode())
        old = {p["ticker"]: p for p in cur.get("positions", [])}
        new = []
        for c, hh in sorted(st["hold"].items(), key=lambda x: -x[1]["value"]):
            if hh["value"] < PUBLIC_MIN_KRW:
                continue
            p = dict(old.get(c) or {"ticker": c, "style": "swing", "since": f"{kst(st['ts']):%Y-%m-%d}"})
            p["avg"] = rp(hh["avg"] or hh["price"])
            p["weight_pct"] = round(hh["weight"], 1)
            new.append(p)
        key = lambda lst: json.dumps([{k: v for k, v in p.items() if k != "weight_pct"} for p in lst], sort_keys=True)
        weights_moved = any(abs((old.get(p["ticker"], {}).get("weight_pct") or 0) - p["weight_pct"]) >= 2 for p in new)
        if key(new) == key(cur.get("positions", [])) and not weights_moved:
            return False
        cur["positions"] = new
        cur["synced_at"] = kst(st["ts"]).isoformat(timespec="seconds")
        body = {"message": "positions sync (server)", "sha": meta["sha"],
                "content": base64.b64encode((json.dumps(cur, ensure_ascii=False, indent=1) + "\n").encode()).decode()}
        r = requests.put(url, headers=h, json=body, timeout=15)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"PUT {r.status_code}")
        log("공개 보유목록 갱신 (종목·평단·비중)")
        return True

    # ── 텔레그램 명령 ──
    def handle(self, text):
        t = (text or "").strip().lower().lstrip("/").split("@")[0]
        cmd = next((c for c, _ in COMMANDS if t.startswith(c)), None)
        if not cmd:
            cmd = next((c for c, ws in WORDS.items() if any(w in t for w in ws)), None)
        if cmd in (None, "help"):
            send("🤖 <b>계좌 비서</b> — 메뉴 버튼이나 한글로 물어보세요\n"
                 + "\n".join(f"/{c} {d}" for c, d in COMMANDS[:-1])
                 + "\n\n예) '계좌', '보유', '승률', '주문', '목표'\n체결·입금은 자동으로 알려드려요.")
            return
        if cmd == "orders":
            try:
                self.sync_fills()
            except BithumbError:
                pass
            send(self.block_orders())
            return
        st = self.snapshot(notify=True)
        curve, flows = self.curve()
        if cmd == "account":
            send(self.full_report(st))
        elif cmd == "holdings":
            send(self.head(st, "보유 코인") + "\n\n" + self.block_holdings(st) + "\n\n" + self.block_holdings_detail(st))
        elif cmd == "perf":
            send(self.block_perf(curve)[0] + "\n\n" + self.block_trades(5))
        elif cmd == "trades":
            send(self.block_trades(15))
        elif cmd == "goal":
            send(self.block_goal(self.goal(curve, flows)) + "\n\n" + self.block_periods(curve, flows))

    def poll_telegram(self, timeout=25):
        off = self.db.get("tg_offset", 0)
        res = tg("getUpdates", offset=off, timeout=timeout, allowed_updates=["message"])
        if not res or not res.get("ok"):
            time.sleep(5)
            return
        for u in res.get("result", []):
            self.db.put("tg_offset", u["update_id"] + 1)
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != TG_CHAT:      # 내 대화방 말고는 무시
                continue
            try:
                self.handle(m.get("text", ""))
            except Exception as e:
                log(f"명령 처리 오류: {type(e).__name__}")
                send(f"⚠️ 처리 중 오류가 났어요 ({esc(type(e).__name__)}). 잠시 후 다시 시도해 주세요.")

    # ── 오류 알림 ──
    def on_error(self, e):
        self.err_count += 1
        log(f"오류: {type(e).__name__} {e if isinstance(e, BithumbError) else ''}")
        if isinstance(e, BithumbError) and e.status in (401, 403) and now_ts() - self.last_err_alert > 6 * 3600:
            self.last_err_alert = now_ts()
            send(f"⚠️ 빗썸 계좌 연결 실패 ({esc(str(e))})\n"
                 "API 키가 만료됐거나, 허용 IP·권한이 바뀌었을 수 있어요. 빗썸 API 관리 화면을 확인해 주세요.")

    # ── 메인 루프 ──
    def run(self):
        tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        first = self.db.get("start_ts") is None
        if first:
            log("첫 실행: 과거 체결 내역을 불러옵니다")
            try:
                self.snapshot(notify=False)
                self.db.x("DELETE FROM pos")          # 과거 체결을 처음부터 다시 쌓아 평단·보유기간 계산
                self.sync_fills(notify=False, backfill=True)
                self.snapshot(notify=False)
                self.db.put("last_daily", f"{kst(now_ts()):%Y-%m-%d}")
                send("✅ <b>계좌 비서 연결 완료</b>\n지금부터 체결·입금을 바로 알려드리고, 매일 "
                     f"{REPORT_HOUR}시에 결산을 보내드려요.\n아래 메뉴(/) 버튼이나 '계좌'라고 보내 보세요.")
                send(self.full_report(self.db.get("last_state")))
            except Exception as e:
                self.on_error(e)
        next_fill = next_snap = 0.0
        while True:
            t = now_ts()
            try:
                if t >= next_fill:
                    next_fill = t + FILL_EVERY
                    self.sync_fills()
                if t >= next_snap:
                    next_snap = t + SNAP_EVERY
                    self.snapshot()
                self.scheduled()
                self.err_count = 0
            except Exception as e:
                self.on_error(e)
                if not isinstance(e, (BithumbError, requests.RequestException)):
                    traceback.print_exc()
            if TG_TOKEN and TG_CHAT:
                self.poll_telegram(timeout=25)
            else:
                time.sleep(30)

    def scheduled(self):
        d = kst(now_ts())
        today = f"{d:%Y-%m-%d}"
        if d.hour >= REPORT_HOUR and self.db.get("last_daily") != today:
            self.db.put("last_daily", today)
            send(self.full_report(self.db.get("last_state"), title="오늘의 결산"))
        if d.day == 1 and d.hour >= 9 and self.db.get("last_monthly") != f"{d:%Y-%m}":
            self.db.put("last_monthly", f"{d:%Y-%m}")
            txt = self.month_report()
            if txt:
                send(txt)


def check():
    """설치 확인: 금액은 출력하지 않습니다."""
    ok = True
    if not (AK and SK):
        print("❌ 빗썸 키가 없습니다")
        return False
    tests = [("자산조회", "/v1/accounts", None), ("주문조회", "/v1/orders", {"state": "wait", "limit": 1}),
             ("입금조회", "/v1/deposits/krw", {"limit": 1}), ("출금조회", "/v1/withdraws/krw", {"limit": 1})]
    for name, path, params in tests:
        try:
            data = bget(path, params)
            extra = ""
            if path == "/v1/accounts":
                extra = f" (코인 {sum(1 for a in data if a.get('currency') != 'KRW')}종 확인)"
            print(f"✅ {name} OK{extra}")
        except BithumbError as e:
            print(f"❌ {name} 실패: {e}")
            if name in ("자산조회", "주문조회"):
                ok = False
    if TG_TOKEN and TG_CHAT:
        r = tg("sendMessage", chat_id=TG_CHAT, text="🔧 계좌 비서 설치 확인 메시지입니다.")
        print("✅ 텔레그램 OK" if r and r.get("ok") else "❌ 텔레그램 전송 실패 (토큰/채팅ID 확인)")
    else:
        print("⚠️ 텔레그램 설정 없음")
    return ok


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "check":
        sys.exit(0 if check() else 1)
    bot = Bot(DB())
    if mode == "report":
        bot.snapshot(notify=False)
        send(bot.full_report(bot.db.get("last_state")))
    else:
        bot.run()
