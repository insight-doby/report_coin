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
import re
import sqlite3
import sys
import threading
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

TP1, TP2, SL = (float(os.environ.get(k, d)) for k, d in
                (("COINBOT_TP1", "3"), ("COINBOT_TP2", "5"), ("COINBOT_SL", "3")))   # 매수 후 안내할 매도·손절 %
LOSS_MIN_KRW = float(os.environ.get("COINBOT_LOSS_MIN_KRW", "300000"))   # 손실 경고 대상: 평가금액 30만원 이상
LOSS_WARN, LOSS_CUT = -5.0, -8.0                                           # 평단 대비 경고 / 정리 권고
BIG_BUY_KRW = float(os.environ.get("COINBOT_BIG_BUY_KRW", "3000000"))      # 큰 금액 매수 경고
MAJOR_KRW = float(os.environ.get("COINBOT_MAJOR_KRW", "1000000"))   # 이 금액 이상 보유만 자세히

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
        body = f"{a / 1e8:.2f}".rstrip("0").rstrip(".") + "억"
    elif a >= 1e5:
        body = f"{a / 1e4:,.1f}".removesuffix(".0") + "만"
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


def qty(v) -> str:
    """수량: 22,260 / 1.5 / 0.0123 (지수 표기 없이)"""
    v = fnum(v)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


def tick_round(v: float, up=True) -> float:
    """주문 가격을 넉넉한 호가 단위(실제 호가 단위의 배수)로 맞춤. 매도는 올림, 손절은 내림."""
    import math
    v = float(v)
    for lim, t in ((1e6, 1000), (1e5, 100), (1e4, 10), (1e3, 5), (100, 1), (10, 0.1), (1, 0.01), (0, 0.0001)):
        if v >= lim:
            n = (math.ceil if up else math.floor)(round(v / t, 6))
            return round(n * t, 6)
    return v


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


def wrap(text: str, width=36, indent="   ") -> str:
    """폰 한 줄 폭(한글 2칸)에 맞춰 띄어쓰기 기준으로 줄바꿈. ' — ' 는 항상 줄바꿈."""
    out = []
    for part in text.split(" — "):
        line = ""
        for word in part.split(" "):
            cand = (line + " " + word).strip()
            if line and sum(2 if ord(ch) > 0x1100 else 1 for ch in cand) > width:
                out.append(line)
                line = word
            else:
                line = cand
        out.append(line)
    return ("\n" + indent).join(out)


def bar(frac, n=10) -> str:
    """진행 막대 ▓▓▓▓░░░░░░ (0~1)."""
    k = max(0, min(n, round((frac or 0) * n)))
    return "▓" * k + "░" * (n - k)


def boxq(lines) -> str:
    """텔레그램 인용 상자 (왼쪽 세로줄로 강조)."""
    return "<blockquote>" + "\n".join(l for l in lines if l is not None) + "</blockquote>"


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


def _tick(markets):
    """시세 조회. 빗썸이 오류를 dict로 돌려주는 경우까지 걸러서 항상 list[dict]로."""
    try:
        data = bget("/v1/ticker", {"markets": markets}, private=False)
    except (BithumbError, requests.RequestException, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("data") if isinstance(data.get("data"), list) else None
    if not isinstance(data, list):
        return None
    return [t for t in data if isinstance(t, dict)]


_MARKETS = {"t": 0.0, "v": set()}


def krw_markets() -> set:
    """원화마켓에 상장된 코인 목록 (6시간 캐시). 상장폐지·에어드롭 잔여 코인을 미리 걸러 조회를 빠르게."""
    if time.time() - _MARKETS["t"] > 6 * 3600 or not _MARKETS["v"]:
        try:
            data = bget("/v1/market/all", {"isDetails": "false"}, private=False)
            v = {str(m.get("market", "")).split("-")[-1] for m in data
                 if isinstance(m, dict) and str(m.get("market", "")).startswith("KRW-")}
            if v:
                _MARKETS.update(t=time.time(), v=v)
        except (BithumbError, requests.RequestException, ValueError):
            pass
    return _MARKETS["v"]


def fetch_prices(coins) -> dict:
    coins = {c for c in coins if c and c != "KRW"}
    listed = krw_markets()
    if listed:
        coins &= listed
    coins = sorted(coins)
    out = {}
    for i in range(0, len(coins), 40):
        chunk = coins[i:i + 40]
        data = _tick(",".join(f"KRW-{c}" for c in chunk))
        if data is None:                         # 상장폐지·원화마켓 없는 코인 하나 때문에 묶음 전체가 실패하는 경우
            data = []
            for c in chunk:
                data += _tick(f"KRW-{c}") or []
        for t in data:
            out[str(t.get("market", "")).split("-")[-1]] = fnum(t.get("trade_price"))
    return out


# ───────────────────────── 저장소 ─────────────────────────
class DB:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.c = sqlite3.connect(path, timeout=30)
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
        CREATE TABLE IF NOT EXISTS realized(uuid TEXT PRIMARY KEY, ts INTEGER, coin TEXT, qty REAL, avg REAL,
                                            px REAL, pnl REAL, pct REAL, fee REAL, hold_h REAL);
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
    chunks, cur = [], ""
    for para in text.split("\n\n"):                 # 문단 단위로 나눠 HTML 태그가 잘리지 않게
        if cur and len(cur) + len(para) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    for c in chunks:
        r = tg("sendMessage", chat_id=TG_CHAT, text=c[:4000], parse_mode="HTML", disable_web_page_preview=True)
        if r is not None and not r.get("ok"):          # 서식 오류면 태그 빼고 다시
            log(f"텔레그램 서식 오류: {str(r.get('description', ''))[:80]}")
            plain = re.sub(r"<[^>]+>", "", c).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            tg("sendMessage", chat_id=TG_CHAT, text=plain[:4000], disable_web_page_preview=True)


COMMANDS = [
    ("account", "계좌 한눈에 (총자산·손익·목표)"),
    ("check", "계좌 점검 (상태 점수·할 일)"),
    ("holdings", "보유 코인별 수익률"),
    ("perf", "매매 성적 (승률·손익비·낙폭)"),
    ("trades", "최근 매도 기록"),
    ("orders", "걸어둔 주문"),
    ("year", "올해 실질 수익 (금액·수익률)"),
    ("goal", "연 30% 목표 진행"),
    ("history", "불러올 수 있는 가장 오래된 기록"),
    ("lifetime", "평생 순수익"),
    ("years", "연도별 성적"),
    ("coins", "코인별 평생 성적"),
    ("realized", "실현손익 (판 것만 · 기간별)"),
    ("export", "매매 기록 파일 내보내기 (엑셀)"),
    ("help", "사용법"),
]
WORDS = {
    "year": ["올해", "실질", "연간"],
    "history": ["언제까지", "오래된", "과거", "히스토리"],
    "coins": ["코인별", "종목별"],
    "realized": ["실현", "확정"],
    "export": ["내보내기", "엑셀", "파일", "기록파일"],
    "years": ["연도", "역대", "해마다"],
    "lifetime": ["평생", "전체기간", "총수익"],
    "account": ["계좌", "자산", "잔고", "전체"], "holdings": ["보유", "코인", "종목"],
    "perf": ["성적", "수익", "승률", "성과"], "trades": ["거래", "매도", "기록", "체결"],
    "orders": ["주문", "지정가", "대기"], "goal": ["목표"], "check": ["점검", "상태", "진단"], "help": ["도움", "help", "start", "메뉴"],
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
        for i in range(3):
            try:
                time.sleep(0.15)
                o = bget("/v1/order", {"uuid": u})
                break
            except BithumbError as e:
                if e.status != 429 or i == 2:
                    raise
                time.sleep(2)
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
        recent = self._orders({"states": ["done", "cancel"]}, pages=60 if backfill else 1)
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
        fresh_after = now_ts() - 30 * 60                 # 30분 넘게 지난 체결은 조용히 기록만
        for d in fills:
            self.apply_fill(d, notify=notify and not backfill and d["ts"] >= fresh_after, prefer_replay=backfill)
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
            old_cost = p["cost"]
            p["qty"] += vol
            p["cost"] += funds + fee
            new_avg = p["cost"] / p["qty"]
            avg_txt = (f"평단 {price_txt(old_avg)} → <b>{price_txt(new_avg)}</b>" if old_avg
                       else f"평단 <b>{price_txt(new_avg)}</b> (새로 매수)")
            warn = ""
            if funds >= BIG_BUY_KRW:
                ok_amt = 100000 / 0.05                               # 손절 -5%에 10만원만 잃는 금액
                warn += ("\n\n" + f"💰 <b>큰 금액 매수</b> ({man(funds)}원)\n"
                         + boxq([f"손절 -5%면 {man(-funds * 0.05, True)}원",
                                 f"10만원만 잃으려면 {man(ok_amt)}원까지가 적당해요"]))
            if old_avg and px < old_avg * 0.995:                  # 손실 중인 코인을 더 산 경우 (물타기)
                hist = self.db.get("avgdown", {}) or {}
                month0 = self.bounds(ts)["month"]
                times = [t for t in hist.get(coin, []) if t >= month0] + [int(ts)]
                hist[coin] = times
                self.db.put("avgdown", hist)
                loss_now = p["qty"] * px - p["cost"]
                warn += ("\n\n" + f"⚠️ <b>손실 중 추가 매수</b> (이번 달 {len(times)}번째)\n"
                        + boxq([f"사기 전 {pct((px / old_avg - 1) * 100)} 손실 중이었어요",
                                f"이 코인에 들어간 돈 {man(old_cost)} → {man(p['cost'])}원",
                                f"지금 손실 {man(loss_now, True)}원 · 평단 회복까지 {pct((new_avg / px - 1) * 100)} 필요",
                                f"손절 -5%면 {man(-p['cost'] * 0.05, True)}원 (평단 {price_txt(new_avg)} 기준)"]))
            msg = (f"🟢 <b>매수 체결 · {coin}</b>\n{price_txt(px)}원 × {qty(vol)}개 = {won(funds)}\n{avg_txt}{warn}\n\n"
                   f"<b>바로 걸어 두기</b> <i>(평단 기준)</i>\n"
                   + boxq([f"매도 +{TP1:g}% <b>{price_txt(tick_round(new_avg * (1 + TP1 / 100)))}원</b> · 절반",
                           f"매도 +{TP2:g}% <b>{price_txt(tick_round(new_avg * (1 + TP2 / 100)))}원</b> · 나머지",
                           f"손절 -{SL:g}% <b>{price_txt(tick_round(new_avg * (1 - SL / 100), up=False))}원</b> · 알림 오면 바로"])
                   )
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
            left = "전량 매도" if p["qty"] <= 1e-12 else f"남은 {qty(p['qty'])}개"
            if pnl is not None:
                msg = (f"{'🔵' if pnl >= 0 else '🔴'} <b>매도 체결 · {coin}</b> ({left})\n"
                       f"{price_txt(px)}원 × {qty(vol)}개 = {won(funds)}\n"
                       f"수익 <b>{pct(pc, 2)}</b> · {man(pnl, True)}원 (수수료 포함)"
                       + (f" · {hold_h / 24:.1f}일 보유" if hold_h else ""))
            else:
                msg = f"🔵 <b>매도 체결 · {coin}</b> ({left})\n{price_txt(px)}원 × {qty(vol)}개 = {won(funds)}"
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
                amt = fnum(it.get("amount"))
                if coin == "KRW":
                    krw = amt
                else:
                    p = prices.get(coin) or fetch_prices([coin]).get(coin, 0)
                    krw = amt * p
                self.db.x("INSERT INTO flows VALUES(?,?,?,?,?,?)", (fid, int(ts), kind, coin, amt, sign * krw))
                new.append((kind, coin, amt, krw))
        self.db.put("flow_perm_missing", missing)
        for kind, coin, amt, krw in new:
            log(f"{kind} 확인 1건")
            if notify:
                what = won(krw) if coin == "KRW" else f"{coin} {qty(amt)}개 (약 {won(krw)})"
                send(f"<b>{kind} 확인</b> · {what}\n수익률 계산에서는 빼고 계산합니다.")

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
        if notify:
            try:
                self.watch_losses(st)
            except Exception as e:
                log(f"손실 점검 실패: {type(e).__name__}")
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

    def realized_rows(self, t_from=0, t_to=None):
        """주식 앱처럼 매도 한 건씩의 확정 손익. 2017년부터 전체 기록 + 그 뒤 실시간 체결."""
        t_to = t_to or 2 ** 40
        life = self.db.get("lifetime") or {}
        cut = int(life.get("computed", 0)) if self.db.q("SELECT 1 FROM realized LIMIT 1") else 0
        rows = self.db.q("SELECT * FROM realized WHERE ts>=? AND ts<? AND ts<=? AND pct IS NOT NULL",
                         (t_from, t_to, cut))
        rows += self.db.q("SELECT * FROM trades WHERE ts>=? AND ts<? AND ts>? AND pct IS NOT NULL",
                          (t_from, t_to, cut))
        return sorted(rows, key=lambda r: r["ts"])

    def stats(self, t_from=0):
        rows = self.realized_rows(t_from)
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
        """연 목표: 올해 실질 수익(1/1부터)이 있으면 그걸로, 없으면 기록 시작 후 1년 기준."""
        if not curve:
            return None
        b = self.bounds()
        st = self.db.get("last_state")
        y = self.ytd(st) if st else None
        if y and y["ret"] is not None and not y["warn"]:
            end = kst(now_ts()).replace(month=12, day=31, hour=23, minute=59).timestamp()
            target = ((1 + GOAL_PCT / 100) ** (y["days"] / 365) - 1) * 100
            months_left = max((end - now_ts()) / 86400 / 30.44, 0.25)
            need_total = (1 + GOAL_PCT / 100) / (1 + y["ret"] / 100)
            m = self.period(b["month"], curve, flows)
            return {"ret": y["ret"], "pnl": y["pnl"], "target": target, "gap": y["ret"] - target, "days": y["days"],
                    "need_m": (need_total ** (1 / months_left) - 1) * 100 if need_total > 0 else None,
                    "month": m["ret"], "month_goal": ((1 + GOAL_PCT / 100) ** (1 / 12) - 1) * 100,
                    "since": b["year"], "end": end}
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


    # ── 올해 실질 수익 (1월 1일 자산을 체결·입출금 기록으로 역산) ──
    def _paged(self, path, params, until_ts, ts_keys=("created_at",), max_pages=60):
        out = []
        for page in range(1, max_pages + 1):
            try:
                data = bget(path, {**params, "page": page, "limit": 100, "order_by": "desc"}) or []
            except BithumbError as e:
                if e.status in (401, 403):
                    raise
                break
            if not isinstance(data, list):
                break
            out += [x for x in data if isinstance(x, dict)]
            oldest = min([parse_ts(next((x.get(k) for k in ts_keys if x.get(k)), None)) for x in data] or [0])
            if len(data) < 100 or (oldest and oldest < until_ts):
                break
        return out

    @staticmethod
    def _close_on(coin, day_end_ts):
        """그날 종가 (KST 자정 직전). 없으면 None."""
        to = kst(day_end_ts).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            data = bget("/v1/candles/days", {"market": f"KRW-{coin}", "to": to, "count": 1}, private=False)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return fnum(data[0].get("trade_price")) or None
        except (BithumbError, requests.RequestException, ValueError):
            pass
        return None

    def compute_ytd(self):
        b = self.bounds()
        y0 = b["year"]
        warn = []
        # 1) 올해 체결: 목록을 1월 1일 이전까지 넘겨보고, 표에 없는 체결은 상세 조회해서 기록만 (알림·손익 계산 없음)
        known = {r[0] for r in self.db.q("SELECT uuid FROM orders")}
        lst = self._paged("/v1/orders", {"states": ["done", "cancel"]}, y0)
        for o in lst:
            u = o.get("uuid")
            if not u or u in known or fnum(o.get("executed_volume")) <= 0:
                continue
            try:
                d = self._detail(u)
            except BithumbError:
                warn.append("일부 체결 상세를 못 불러옴")
                continue
            if d["volume"] > 0 and d["market"].startswith("KRW-"):
                self.db.x("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?,?)",
                          (d["uuid"], int(d["ts"]), d["coin"], d["side"], d["volume"], d["funds"], d["fee"]))
        if len(lst) >= 60 * 100:
            warn.append("체결 기록이 너무 많아 일부만 반영")
        orders = self.db.q("SELECT * FROM orders WHERE ts>=?", (y0,))
        # 2) 올해 입출금
        flows = []
        for path, sign in (("/v1/deposits/krw", 1), ("/v1/withdraws/krw", -1), ("/v1/deposits", 1), ("/v1/withdraws", -1)):
            try:
                items = self._paged(path, {}, y0, ts_keys=("done_at", "created_at"))
            except BithumbError:
                warn.append("입출금 조회 권한 없음 — 입금이 수익으로 잡힐 수 있음")
                continue
            for it in items:
                if str(it.get("state", "")).upper() not in DONE_STATES:
                    continue
                ts = parse_ts(it.get("done_at") or it.get("created_at"))
                if ts < y0:
                    continue
                flows.append({"ts": ts, "coin": str(it.get("currency", "KRW")).upper(), "amt": fnum(it.get("amount")),
                              "fee": fnum(it.get("fee")), "sign": sign})
        # 3) 지금 잔고에서 거꾸로 1월 1일 잔고 계산
        cash, hold = self.accounts()
        qty0 = {c: h["qty"] for c, h in hold.items()}
        fees = 0.0
        for o in orders:
            c, v, f, fee = o["coin"], o["volume"], o["funds"], o["fee"] or 0
            fees += fee
            if o["side"] == "bid":
                cash += f + fee
                qty0[c] = qty0.get(c, 0) - v
            else:
                cash -= f - fee
                qty0[c] = qty0.get(c, 0) + v
        dep = wd = 0.0
        fl_krw = []
        for fl in flows:
            if fl["coin"] == "KRW":
                val = fl["amt"]
                cash += -fl["amt"] if fl["sign"] > 0 else fl["amt"] + fl["fee"]
            else:
                qty0[fl["coin"]] = qty0.get(fl["coin"], 0) + (-fl["amt"] if fl["sign"] > 0 else fl["amt"] + fl["fee"])
                day_end = kst(fl["ts"]).replace(hour=0, minute=0, second=0).timestamp() + 86400
                val = fl["amt"] * (self._close_on(fl["coin"], day_end) or 0)
            fl_krw.append((fl["ts"], fl["sign"] * val))
            if fl["sign"] > 0:
                dep += val
            else:
                wd += val
        # 4) 1월 1일 시가로 평가
        listed = krw_markets()
        e0 = max(cash, 0.0)
        if cash < -1000:
            warn.append("1월 1일 현금 추정이 음수 — 기록 일부 누락 가능")
        for c, q in qty0.items():
            if q < 0:
                if q * (fetch_prices([c]).get(c) or 0) < -5000:
                    warn.append(f"{c} 수량 역산 불일치")
                continue
            if q <= 0 or (listed and c not in listed):
                continue
            p0 = self._close_on(c, y0)
            if p0:
                e0 += q * p0
        res = {"y0": y0, "e0": e0, "flows": fl_krw, "dep": dep, "wd": wd, "fees": fees,
               "computed": now_ts(), "warn": sorted(set(warn)), "n_orders": len(orders)}
        self.db.put("ytd", res)
        log(f"올해 실질 수익 기준 계산 완료 (체결 {len(orders)}건, 입출금 {len(flows)}건)")
        return res

    def ytd(self, st):
        """올해 수익 금액·수익률 (수정 디에츠: 입출금 시점 가중)."""
        y = self.db.get("ytd")
        if not y or y.get("y0") != self.bounds()["year"] or not st:
            return None
        flows = list(map(tuple, y["flows"]))
        flows += [(r["ts"], r["krw"]) for r in self.db.q("SELECT ts,krw FROM flows WHERE ts>?", (y["computed"],))]
        now = st["ts"]
        span = max(now - y["y0"], 1)
        net = sum(k for _, k in flows)
        pnl = st["total"] - y["e0"] - net
        base = y["e0"] + sum(k * (now - ts) / span for ts, k in flows)
        dep = sum(k for _, k in flows if k > 0)
        wd = -sum(k for _, k in flows if k < 0)
        return {"pnl": pnl, "ret": pnl / base * 100 if base > 0 else None, "e0": y["e0"], "now": st["total"],
                "net": net, "dep": dep, "wd": wd, "fees": y["fees"], "warn": y.get("warn", []),
                "days": span / 86400, "year": kst(y["y0"]).year}

    def block_ytd(self, st):
        y = self.ytd(st)
        if not y:
            return "<b>올해 실질 수익</b>\n계산 준비 중이에요 (처음 한 번 몇 분 걸려요)"
        lines = [f"<b>{y['year']}년 실질 수익</b>",
                 "<i>1/1~지금 · 입출금 제외</i>",
                 boxq([f"{dot(y['pnl'])} 수익 금액 <b>{man(y['pnl'], True)}원</b>",
                        f"수익률 <b>{pct(y['ret'])}</b>",
                        (f"{bar(max(y['ret'] or 0, 0) / GOAL_PCT)} 연 {GOAL_PCT:.0f}% 중 "
                         f"{max(y['ret'] or 0, 0) / GOAL_PCT * 100:.0f}%") if y["ret"] is not None else None]),
                 f"1/1 자산(추정) {man(y['e0'])}원",
                 f"지금 자산 {man(y['now'])}원",
                 f"올해 입금 {man(y['dep'])}원",
                 f"올해 출금 {man(y['wd'])}원",
                 f"올해 수수료 {won(y['fees'])}"]
        if y["warn"]:
            lines.append("<i>⚠️ " + esc(" · ".join(y["warn"])) + "</i>")
        return "\n".join(lines)


    # ── 평생 순수익 · 연도별 성적 (전체 체결·입출금으로 매년 1/1 자산 역산) ──
    def compute_lifetime(self, notify=False):
        t0 = now_ts()
        warn = set()
        # 1) 전체 체결 (표에 없는 것만 상세 조회, 기록만)
        known = {r[0] for r in self.db.q("SELECT uuid FROM orders")}
        lst = self._paged("/v1/orders", {"states": ["done", "cancel"]}, 0, max_pages=400)
        if len(lst) >= 400 * 100:
            warn.add("체결 기록이 4만 건을 넘어 일부만 반영")
        for o in lst:
            u = o.get("uuid")
            if not u or u in known or fnum(o.get("executed_volume")) <= 0:
                continue
            try:
                d = self._detail(u)
            except BithumbError:
                warn.add("일부 체결 상세를 못 불러옴")
                continue
            if d["volume"] > 0 and d["market"].startswith("KRW-"):
                self.db.x("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?,?)",
                          (d["uuid"], int(d["ts"]), d["coin"], d["side"], d["volume"], d["funds"], d["fee"]))
        orders = [dict(r) for r in self.db.q("SELECT * FROM orders ORDER BY ts")]
        # 2) 전체 입출금
        flows = []
        for path, sign in (("/v1/deposits/krw", 1), ("/v1/withdraws/krw", -1), ("/v1/deposits", 1), ("/v1/withdraws", -1)):
            try:
                items = self._paged(path, {}, 0, ts_keys=("done_at", "created_at"), max_pages=200)
            except BithumbError:
                warn.add("입출금 조회 권한 없음")
                continue
            for it in items:
                if str(it.get("state", "")).upper() not in DONE_STATES:
                    continue
                ts = parse_ts(it.get("done_at") or it.get("created_at"))
                if ts:
                    flows.append({"ts": ts, "coin": str(it.get("currency", "KRW")).upper(), "amt": fnum(it.get("amount")),
                                  "fee": fnum(it.get("fee")), "sign": sign})
        flows.sort(key=lambda f: f["ts"])
        closes = {}

        def close(coin, ts):
            day = kst(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            key = (coin, day)
            if key not in closes:
                closes[key] = self._close_on(coin, day)
            return closes[key]

        for f in flows:
            if f["coin"] == "KRW":
                f["krw"] = f["amt"]
            else:
                p = close(f["coin"], f["ts"] + 86400)
                if not p:
                    warn.add("가격을 못 구한 코인 입출금 있음")
                f["krw"] = f["amt"] * (p or 0)
        self.db.put("life_flows", flows)
        # 3) 지금 잔고에서 시간을 거꾸로 돌리며 매년 1월 1일 잔고 기록
        cash, hold = self.accounts()
        qty = {c: h["qty"] for c, h in hold.items()}
        first_ts = min([o["ts"] for o in orders] + [f["ts"] for f in flows] or [now_ts()])
        y_now = kst(now_ts()).year
        years = list(range(kst(first_ts).year, y_now + 1))
        bounds = {y: datetime(y, 1, 1, tzinfo=KST).timestamp() for y in years}
        events = [("o", o["ts"], o) for o in orders] + [("f", f["ts"], f) for f in flows]
        events.sort(key=lambda e: -e[1])
        snaps, todo = {}, sorted(years, reverse=True)
        for kind, ts, e in events:
            while todo and ts < bounds[todo[0]]:
                snaps[todo.pop(0)] = (cash, dict(qty))
            if kind == "o":
                if e["side"] == "bid":
                    cash += e["funds"] + (e["fee"] or 0)
                    qty[e["coin"]] = qty.get(e["coin"], 0) - e["volume"]
                else:
                    cash -= e["funds"] - (e["fee"] or 0)
                    qty[e["coin"]] = qty.get(e["coin"], 0) + e["volume"]
            elif e["coin"] == "KRW":
                cash += -e["amt"] if e["sign"] > 0 else e["amt"] + e["fee"]
            else:
                qty[e["coin"]] = qty.get(e["coin"], 0) + (-e["amt"] if e["sign"] > 0 else e["amt"] + e["fee"])
        for y in todo:
            snaps[y] = (cash, dict(qty))
        listed = krw_markets()
        start_val, year_warn = {}, {y: set() for y in years}
        for y in years:
            c0, q0 = snaps[y]
            if c0 < -10000:
                year_warn[y].add("현금 역산 불일치")
            v = max(c0, 0.0)
            for coin, q in q0.items():
                if q <= 1e-12:
                    continue
                p = close(coin, bounds[y])
                if p:
                    v += q * p
                elif not listed or coin not in listed or q > 0:
                    year_warn[y].add("가격 없는 코인 있음")
            start_val[y] = v
        st = self.state()
        # 4) 연도별 매도 성적 (평균단가로 처음부터 다시 계산)
        pos, per = {}, {y: {"n": 0, "w": 0, "real": 0.0, "fee": 0.0, "nb": 0} for y in years}
        real_rows = []
        coins, opened = {}, {}

        def cs(cn):
            return coins.setdefault(cn, {"n": 0, "w": 0, "real": 0.0, "fee": 0.0, "buy": 0.0, "hold": [],
                                         "first": None, "last": None})
        evs = [("o", o["ts"], o) for o in orders] + [("f", f["ts"], f) for f in flows if f["coin"] != "KRW"]
        evs.sort(key=lambda e: e[1])
        for kind, ts, e in evs:
            y = kst(ts).year
            if y not in per:
                continue
            q, cost = pos.get(e["coin"], (0.0, 0.0))
            if kind == "f":
                if e["sign"] > 0:
                    pos[e["coin"]] = (q + e["amt"], cost + e["krw"])
                elif q > 0:
                    take = min(q, e["amt"] + e["fee"])
                    pos[e["coin"]] = (q - take, cost * (1 - take / q))
                continue
            per[y]["fee"] += e["fee"] or 0
            cc = cs(e["coin"])
            cc["fee"] += e["fee"] or 0
            cc["first"] = cc["first"] or ts
            cc["last"] = ts
            if e["side"] == "bid":
                per[y]["nb"] += 1
                cc["buy"] += e["funds"]
                if q <= 1e-12:
                    opened[e["coin"]] = ts
                pos[e["coin"]] = (q + e["volume"], cost + e["funds"] + (e["fee"] or 0))
            else:
                if q >= e["volume"] * 0.98 and q > 0:
                    basis = cost / q * e["volume"]
                    pnl = e["funds"] - (e["fee"] or 0) - basis
                    per[y]["n"] += 1
                    per[y]["w"] += pnl > 0
                    per[y]["real"] += pnl
                    cc["n"] += 1
                    cc["w"] += pnl > 0
                    cc["real"] += pnl
                    hold_h = (ts - opened[e["coin"]]) / 3600 if opened.get(e["coin"]) else None
                    if hold_h is not None:
                        cc["hold"].append(hold_h / 24)
                    real_rows.append((e["uuid"], int(ts), e["coin"], e["volume"], cost / q,
                                      e["funds"] / e["volume"] if e["volume"] else 0, pnl,
                                      pnl / basis * 100 if basis else None, e["fee"] or 0, hold_h))
                take = min(q, e["volume"])
                pos[e["coin"]] = (q - take, cost * (1 - take / q) if q > 0 else 0.0)
                if pos[e["coin"]][0] * (e["funds"] / e["volume"] if e["volume"] else 0) < DUST_KRW:
                    pos[e["coin"]] = (0.0, 0.0)
                    opened.pop(e["coin"], None)
        # 5) 연도별 수익
        rows = []
        for i, y in enumerate(years):
            a = bounds[y]
            end_ts = bounds[years[i + 1]] if i + 1 < len(years) else st["ts"]
            e0 = start_val[y]
            e1 = start_val[years[i + 1]] if i + 1 < len(years) else st["total"]
            fl = [(f["ts"], f["sign"] * f["krw"]) for f in flows if a <= f["ts"] < end_ts]
            net = sum(k for _, k in fl)
            span = max(end_ts - a, 1)
            base = e0 + sum(k * (end_ts - ts) / span for ts, k in fl)
            pnl = e1 - e0 - net
            rows.append({"year": y, "e0": e0, "e1": e1, "pnl": pnl, "ret": pnl / base * 100 if base > 1000 else None,
                         "dep": sum(k for _, k in fl if k > 0), "wd": -sum(k for _, k in fl if k < 0),
                         "n": per[y]["n"], "wr": per[y]["w"] / per[y]["n"] * 100 if per[y]["n"] else None,
                         "real": per[y]["real"], "fee": per[y]["fee"], "warn": sorted(year_warn[y])})
        dep = sum(f["krw"] for f in flows if f["sign"] > 0)
        wd = sum(f["krw"] for f in flows if f["sign"] < 0)
        first_dep = min([f["ts"] for f in flows if f["sign"] > 0 and f["coin"] == "KRW"] or [0])
        first_ord = orders[0]["ts"] if orders else 0
        if first_ord and (not first_dep or first_dep > first_ord + 86400 * 2):
            warn.add("입금 기록이 첫 체결보다 늦게 시작 — 초기 입금 일부가 빠졌을 수 있음")
        self.db.c.execute("DELETE FROM realized")
        self.db.c.executemany("INSERT OR REPLACE INTO realized VALUES(?,?,?,?,?,?,?,?,?,?)", real_rows)
        self.db.c.commit()
        coin_rows = []
        for cn, cc in coins.items():
            h = st["hold"].get(cn)
            unreal = (h["pnl"] or 0) if h else 0.0
            coin_rows.append({"coin": cn, "real": cc["real"], "unreal": unreal, "total": cc["real"] + unreal,
                              "n": cc["n"], "wr": cc["w"] / cc["n"] * 100 if cc["n"] else None, "fee": cc["fee"],
                              "buy": cc["buy"], "hold_d": sum(cc["hold"]) / len(cc["hold"]) if cc["hold"] else None,
                              "held": bool(h), "first": cc["first"], "last": cc["last"]})
        for cn, h in st["hold"].items():
            if cn not in coins:
                coin_rows.append({"coin": cn, "real": 0.0, "unreal": h["pnl"] or 0, "total": h["pnl"] or 0, "n": 0,
                                  "wr": None, "fee": 0.0, "buy": 0.0, "hold_d": None, "held": True,
                                  "first": None, "last": None})
        coin_rows.sort(key=lambda r: -r["total"])
        res = {"computed": now_ts(), "first": first_ts, "dep": dep, "wd": wd, "now": st["total"], "coins": coin_rows,
               "pnl": st["total"] - (dep - wd), "years": rows, "warn": sorted(warn), "n_orders": len(orders),
               "n_flows": len(flows)}
        self.db.put("lifetime", res)
        log(f"평생 성적 계산 완료: 체결 {len(orders)}건 · 입출금 {len(flows)}건 · {now_ts() - t0:.0f}초")
        if notify:
            send("✅ <b>평생 기록 계산이 끝났어요</b> — 평생 순수익 · 연도별 · 코인별 성적을 차례로 보내드려요.")
            self.send_life("lifetime")
            self.send_life("years")
            self.send_life("coins")
        return res

    def send_life(self, which):
        r = self.db.get("lifetime")
        if which == "lifetime":
            send_photo(chart_lifetime(r))
            send(self.block_lifetime())
        elif which == "years":
            send_photo(chart_years(r))
            send(self.block_years())
        else:
            send_photo(chart_coins(r))
            send(self.block_coins())

    def start_lifetime(self, notify=True):
        """오래 걸리므로 별도 스레드·별도 DB 연결에서 계산 (봇은 계속 응답)."""
        if getattr(self, "_life_thread", None) and self._life_thread.is_alive():
            return False

        def job():
            try:
                Bot(DB(self.db.path)).compute_lifetime(notify=notify)
            except Exception as e:
                log(f"평생 성적 계산 실패: {type(e).__name__} {e}")
                if notify:
                    send(f"⚠️ 평생 성적 계산 중 오류 ({esc(type(e).__name__)}). 내일 다시 시도할게요.")
        self._life_thread = threading.Thread(target=job, daemon=True)
        self._life_thread.start()
        return True

    LIFE_WAIT = "계산 중이에요. 처음 한 번은 기록이 많아 몇 분~십여 분 걸려요. 끝나면 바로 보내드릴게요."

    def block_lifetime(self):
        r = self.db.get("lifetime")
        if not r:
            return "<b>평생 순수익</b>\n" + self.LIFE_WAIT
        net_in = r["dep"] - r["wd"]
        lines = ["<b>평생 순수익</b>", f"<i>{kst(r['first']):%Y-%m} ~ 지금 · 입출금 기준</i>",
                 f"총 입금 {won(r['dep'])}",
                 f"총 출금 {won(r['wd'])}",
                 f"순투입 {won(net_in)}",
                 f"지금 자산 {won(r['now'])}",
                 "",
                 boxq([f"{dot(r['pnl'])} 평생 순수익 <b>{man(r['pnl'], True)}원</b>",
                        (f"순투입 대비 <b>{pct(r['pnl'] / net_in * 100)}</b>" if net_in > 0 else None)]),
                 f"매매 {r['n_orders']:,}건 · 입출금 {r['n_flows']:,}건 기준"]
        if r["warn"]:
            lines.append("<i>⚠️ " + esc(" · ".join(r["warn"])) + "</i>")
        lines.append(f"<i>{kst(r['computed']):%m/%d %H:%M} 계산 · 매주 일요일 새로 계산</i>")
        return "\n".join(lines)

    def block_years(self):
        r = self.db.get("lifetime")
        if not r:
            return "<b>연도별 성적</b>\n" + self.LIFE_WAIT
        rows = []
        for y in r["years"]:
            mark = "*" if y["warn"] else " "
            wr = f"{y['wr']:.0f}%" if y["wr"] is not None else "-"
            rows.append(f"{y['year']}{mark}{pct(y['ret']):>8} {y['n']:>4} {wr:>4} {man(y['pnl'], True):>9}")
        lines = ["<b>연도별 성적</b>", "<i>수익률 · 매도 수 · 승률 · 수익금액</i>",
                 "<pre>" + esc("\n".join(rows)) + "</pre>"]
        good = [y for y in r["years"] if y["ret"] is not None]
        if good:
            best = max(good, key=lambda y: y["ret"])
            worst = min(good, key=lambda y: y["ret"])
            hit = sum(1 for y in good if y["ret"] >= GOAL_PCT)
            lines.append(f"최고 {best['year']}년 {pct(best['ret'])}")
            lines.append(f"최저 {worst['year']}년 {pct(worst['ret'])}")
            lines.append(f"연 {GOAL_PCT:.0f}% 달성 {hit}/{len(good)}년")
        fees = sum(y["fee"] for y in r["years"])
        lines.append(f"수수료 합계 {won(fees)}")
        lines.append("<i>입출금 제외 · 1/1 자산은 기록으로 역산</i>")
        if any(y["warn"] for y in r["years"]):
            lines.append("<i>* 상장폐지 코인 가격 등을 못 구해 오차가 있을 수 있는 해</i>")
        return "\n".join(lines)

    def block_coins(self, top=5):
        r = self.db.get("lifetime")
        if not r:
            return "<b>코인별 평생 성적</b>\n" + self.LIFE_WAIT
        rows = [c for c in r.get("coins", []) if c["n"] or c["held"]]
        if not rows:
            return "<b>코인별 평생 성적</b>\n매도 기록이 없어요"

        def line(c):
            wr = f"{c['wr']:.0f}%" if c["wr"] is not None else "-"
            hd = f"{c['hold_d']:.0f}d" if c["hold_d"] is not None else "-"
            mark = "•" if c["held"] else " "
            return f"{mark}{c['coin']:<7}{c['n']:>4} {wr:>4} {hd:>4} {man(c['total'], True):>9}"

        win = [c for c in rows if c["total"] > 0][:top]
        lose = sorted([c for c in rows if c["total"] < 0], key=lambda c: c["total"])[:top]
        lines = ["<b>코인별 평생 성적</b>", "<i>매도 수 · 승률 · 평균 보유 · 총 손익</i>"]
        if win:
            lines += ["\n🟢 <b>가장 많이 번 코인</b>", "<pre>" + esc("\n".join(line(c) for c in win)) + "</pre>"]
        if lose:
            lines += ["🔴 <b>가장 많이 잃은 코인</b>", "<pre>" + esc("\n".join(line(c) for c in lose)) + "</pre>"]
        tot_real = sum(c["real"] for c in rows)
        n_coin = len([c for c in rows if c["n"]])
        lines.append(f"매매한 코인 {n_coin}개")
        lines.append(f"실현 손익 합계 {man(tot_real, True)}원")
        lines.append("<i>• 보유 중 · 총 손익 = 실현 + 평가</i>")
        return "\n".join(lines)


    # ── 매매 기록 내보내기 (압축 파일: 엑셀로 여는 CSV 여러 개) ──
    def _monthly_closes(self, coin):
        """월봉 종가 {'YYYY-MM': 종가}. 상장폐지 등으로 없으면 빈 dict."""
        out = {}
        try:
            data = bget("/v1/candles/months", {"market": f"KRW-{coin}", "count": 200}, private=False)
            for r in data if isinstance(data, list) else []:
                if isinstance(r, dict) and r.get("candle_date_time_kst"):
                    out[str(r["candle_date_time_kst"])[:7]] = fnum(r.get("trade_price"))
        except (BithumbError, requests.RequestException, ValueError):
            pass
        return out

    def build_export(self):
        import csv, io, zipfile
        if not self.db.get("lifetime") or self.db.get("life_flows") is None:
            self.compute_lifetime(notify=False)
        life = self.db.get("lifetime") or {}
        flows = self.db.get("life_flows") or []
        orders = [dict(r) for r in self.db.q("SELECT * FROM orders ORDER BY ts")]
        real = {r["uuid"]: dict(r) for r in self.realized_rows(0)}
        st = self.state()
        listed = krw_markets()
        files = {}

        def table(name, header, rows):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header)
            for r in rows:
                w.writerow(["" if v is None else (round(v, 8) if isinstance(v, float) else v) for v in r])
            files[name] = "﻿" + buf.getvalue()          # 엑셀에서 한글이 안 깨지게

        day = lambda ts: kst(ts).strftime("%Y-%m-%d %H:%M")
        # 1) 매매 기록 + 매도별 실현손익
        table("2_매매기록.csv", ["일시", "코인", "구분", "체결가", "수량", "금액", "수수료", "평단(매도 시)", "실현손익", "수익률%", "보유일"],
              [[day(o["ts"]), o["coin"], "매수" if o["side"] == "bid" else "매도",
                o["funds"] / o["volume"] if o["volume"] else None, o["volume"], o["funds"], o["fee"],
                (real.get(o["uuid"]) or {}).get("avg"), (real.get(o["uuid"]) or {}).get("pnl"),
                (real.get(o["uuid"]) or {}).get("pct"),
                ((real.get(o["uuid"]) or {}).get("hold_h") or 0) / 24 if (real.get(o["uuid"]) or {}).get("hold_h") else None]
               for o in orders])
        rr = sorted(real.values(), key=lambda r: r["ts"])
        table("3_매도별_실현손익.csv", ["일시", "코인", "수량", "평단", "매도가", "실현손익", "수익률%", "수수료", "보유일"],
              [[day(r["ts"]), r["coin"], r["qty"], r["avg"], r["px"], r["pnl"], r["pct"], r["fee"],
                r["hold_h"] / 24 if r["hold_h"] else None] for r in rr])
        # 2) 입출금
        kind = {(1, True): "원화 입금", (-1, True): "원화 출금", (1, False): "코인 입금", (-1, False): "코인 출금"}
        table("4_입출금.csv", ["일시", "구분", "코인", "수량", "원화 환산", "수수료"],
              [[day(f["ts"]), kind[(f["sign"], f["coin"] == "KRW")], f["coin"], f["amt"], f.get("krw"), f.get("fee")]
               for f in sorted(flows, key=lambda f: f["ts"])])
        # 3) 코인별 매수·매도 합계와 남은 원가 (평균단가로 처음부터 다시 쌓기)
        pos, agg = {}, {}
        evs = [("o", o["ts"], o) for o in orders] + [("f", f["ts"], f) for f in flows if f["coin"] != "KRW"]
        for k, ts, e in sorted(evs, key=lambda x: x[1]):
            q, cost = pos.get(e["coin"], (0.0, 0.0))
            a = agg.setdefault(e["coin"], {"buy": 0.0, "sell": 0.0, "fee": 0.0, "nb": 0, "ns": 0, "in": 0.0, "out": 0.0,
                                           "first": ts, "last": ts})
            a["last"] = ts
            if k == "f":
                if e["sign"] > 0:
                    a["in"] += e.get("krw") or 0
                    pos[e["coin"]] = (q + e["amt"], cost + (e.get("krw") or 0))
                else:
                    a["out"] += e.get("krw") or 0
                    take = min(q, e["amt"] + (e.get("fee") or 0))
                    pos[e["coin"]] = (q - take, cost * (1 - take / q) if q > 0 else 0.0)
                continue
            a["fee"] += e["fee"] or 0
            if e["side"] == "bid":
                a["buy"] += e["funds"]
                a["nb"] += 1
                pos[e["coin"]] = (q + e["volume"], cost + e["funds"] + (e["fee"] or 0))
            else:
                a["sell"] += e["funds"]
                a["ns"] += 1
                take = min(q, e["volume"])
                pos[e["coin"]] = (q - take, cost * (1 - take / q) if q > 0 else 0.0)
        coins_life = {c["coin"]: c for c in life.get("coins", [])}
        delisted = []
        rows = []
        for cn, a in sorted(agg.items(), key=lambda x: -(x[1]["sell"] - x[1]["buy"])):
            h = st["hold"].get(cn)
            q, cost = pos.get(cn, (0.0, 0.0))
            gone = bool(listed) and cn not in listed
            cl = coins_life.get(cn) or {}
            if gone and (q > 0 or a["buy"] > 0):
                delisted.append({"coin": cn, "buy": a["buy"], "sell": a["sell"], "fee": a["fee"], "left_qty": q,
                                 "left_cost": cost,
                                 "first": a["first"], "last": a["last"]})
            rows.append([cn, "상장폐지/원화마켓 없음" if gone else ("보유 중" if h else ""), a["nb"], a["buy"], a["ns"], a["sell"],
                         a["fee"], cl.get("real"), (h or {}).get("pnl"), cl.get("wr"), cl.get("hold_d"),
                         day(a["first"]), day(a["last"])])
        table("6_코인별_요약.csv", ["코인", "상태", "매수 횟수", "총 매수액", "매도 횟수", "총 매도액", "수수료",
                                "실현손익", "평가손익(보유 중)", "승률%", "평균 보유일", "처음 거래", "마지막 거래"], rows)
        table("8_상장폐지_코인.csv", ["코인", "총 매수액", "총 매도액", "수수료", "남은 수량(가치 0)", "남은 원가(사라진 돈)", "처음 거래", "마지막 거래"],
              [[d["coin"], d["buy"], d["sell"], d["fee"], d["left_qty"], d["left_cost"], day(d["first"]), day(d["last"])]
               for d in sorted(delisted, key=lambda d: -d["left_cost"])])
        # 4) 보유 현황
        table("5_보유현황.csv", ["코인", "수량", "평단(빗썸)", "현재가", "평가금액", "평가손익", "수익률%", "비중%"],
              [[c, h["qty"], h["avg"], h["price"], h["value"], h["pnl"], h["pct"], h["weight"]]
               for c, h in sorted(st["hold"].items(), key=lambda x: -x[1]["value"])]
              + [["현금(원화)", None, None, None, st["cash"], None, None, st["cash"] / st["total"] * 100 if st["total"] else None]])
        # 5) 연도별
        table("7_연도별_요약.csv", ["연도", "1/1 자산(추정)", "연말 자산(추정)", "입금", "출금", "순수익(입출금 제외)", "수익률%",
                                "매도 수", "승률%", "실현손익", "수수료", "주의"],
              [[y["year"], y["e0"], y["e1"], y["dep"], y["wd"], y["pnl"], y["ret"], y["n"], y["wr"], y["real"], y["fee"],
                " · ".join(y["warn"])] for y in life.get("years", [])])
        # 6) 월별 자산 흐름 + 비트코인 비교
        months = self.monthly_curve(orders, flows)
        table("9_월별_자산흐름.csv", ["월", "월초 자산(추정)", "월말 자산(추정)", "입금", "출금", "손익(입출금 제외)", "수익률%",
                                "누적 수익률%", "고점 대비%", "비트코인 수익률%", "비트 누적%", "주의"],
              [[m["month"], m["e0"], m["e1"], m["dep"], m["wd"], m["pnl"], m["ret"], m["cum"], m["dd"], m["btc"], m["btc_cum"],
                m["warn"]] for m in months])
        # 7) 손익 맞추기
        realized_sum = sum(r["pnl"] for r in rr)
        unreal = sum(h["pnl"] or 0 for h in st["hold"].values())
        gone_cost = sum(d["left_cost"] for d in delisted)
        unknown = [o for o in orders if o["side"] == "ask" and o["uuid"] not in real]
        net = life.get("pnl", st["total"] - (life.get("dep", 0) - life.get("wd", 0)))
        other = net - realized_sum - unreal + gone_cost
        rec = [["평생 순수익 (지금 자산 − 순투입)", net, "넣은 돈 대비 지금 계좌 전체"],
               ["① 실현손익 (판 것, 평단 아는 매도)", realized_sum, f"매도 {len(rr)}건"],
               ["② 평가손익 (지금 들고 있는 것)", unreal, f"{len(st['hold'])}종목"],
               ["③ 상장폐지 등으로 사라진 원가", -gone_cost, f"{len(delisted)}종목 · 팔지 못하고 가치가 0이 된 몫"],
               ["④ 기타 (나머지 차이)", other, "평단 모르는 매도·에어드롭·코인 출금 뒤 손익·기록 누락 등"],
               ["", None, ""],
               ["참고: 평단을 몰라 ①에서 빠진 매도", sum(o["funds"] for o in unknown), f"{len(unknown)}건 (매도 금액 합계)"],
               ["참고: 총 입금", life.get("dep"), ""], ["참고: 총 출금", life.get("wd"), ""],
               ["참고: 지금 자산", st["total"], ""],
               ["참고: 총 수수료", sum(o["fee"] or 0 for o in orders), f"체결 {len(orders)}건"]]
        table("1_손익_맞추기.csv", ["항목", "금액(원)", "설명"], rec)
        files["0_읽어보기.txt"] = (
            "계좌 비서 매매 기록 (빗썸)\n"
            f"만든 시각: {kst(now_ts()):%Y-%m-%d %H:%M}\n\n"
            "1_손익_맞추기 : 평생 순수익 = ① 실현 + ② 평가 + ③ 상장폐지 + ④ 기타\n"
            "2_매매기록 : 모든 체결 (매도에는 평단·실현손익 표시)\n"
            "3_매도별_실현손익 : 판 것마다 손익·수익률·보유일\n"
            "4_입출금 : 원화·코인 입출금 (코인은 그날 종가로 원화 환산)\n"
            "5_보유현황 : 지금 들고 있는 코인과 현금\n"
            "6_코인별_요약 : 코인마다 총 매수·매도, 실현·평가, 승률\n"
            "7_연도별_요약 : 해마다 순수익·실현손익·입출금·수수료\n"
            "8_상장폐지_코인 : 원화마켓에서 사라진 코인과 사라진 원가\n"
            "9_월별_자산흐름 : 매달 자산·수익률·고점 대비 낙폭, 같은 달 비트코인 수익률\n\n"
            "주의: 과거 자산은 체결·입출금 기록으로 거꾸로 계산한 추정치입니다. 상장폐지 코인 가격 등은 0으로 봅니다.\n"
            "금액이 들어 있는 파일이니 공유에 주의하세요.\n")
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, text in files.items():
                z.writestr(name, text.encode("utf-8"))
        summary = {"net": net, "realized": realized_sum, "unreal": unreal, "gone": gone_cost, "other": other,
                   "n_delisted": len(delisted), "top_gone": sorted(delisted, key=lambda d: -d["left_cost"])[:3],
                   "months": months}
        return zbuf.getvalue(), summary

    def monthly_curve(self, orders, flows):
        """매달 1일 자산을 지금 잔고에서 거꾸로 계산 + 월말 종가로 평가."""
        cash, hold = self.accounts()
        qty = {c: h["qty"] for c, h in hold.items()}
        if not orders and not flows:
            return []
        first = min([o["ts"] for o in orders] + [f["ts"] for f in flows])
        d0 = kst(first).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        bounds, d = [], d0
        while d.timestamp() <= now_ts():
            bounds.append(d)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        events = [("o", o["ts"], o) for o in orders] + [("f", f["ts"], f) for f in flows]
        events.sort(key=lambda e: -e[1])
        snaps, todo = {}, list(reversed(bounds))
        for k, ts, e in events:
            while todo and ts < todo[0].timestamp():
                snaps[todo.pop(0).strftime("%Y-%m")] = (cash, dict(qty))
            if k == "o":
                if e["side"] == "bid":
                    cash += e["funds"] + (e["fee"] or 0)
                    qty[e["coin"]] = qty.get(e["coin"], 0) - e["volume"]
                else:
                    cash -= e["funds"] - (e["fee"] or 0)
                    qty[e["coin"]] = qty.get(e["coin"], 0) + e["volume"]
            elif e["coin"] == "KRW":
                cash += -e["amt"] if e["sign"] > 0 else e["amt"] + (e.get("fee") or 0)
            else:
                qty[e["coin"]] = qty.get(e["coin"], 0) + (-e["amt"] if e["sign"] > 0 else e["amt"] + (e.get("fee") or 0))
        for b in todo:
            snaps[b.strftime("%Y-%m")] = (cash, dict(qty))
        coins = {c for _, (_, q) in snaps.items() for c, v in q.items() if v > 1e-12} | {"BTC"}
        mc = {c: self._monthly_closes(c) for c in sorted(coins)}

        def prev_month(key):
            y, m = int(key[:4]), int(key[5:])
            return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"

        def value(key):
            c0, q0 = snaps[key]
            v, miss = max(c0, 0.0), False
            for c, q in q0.items():
                if q <= 1e-12:
                    continue
                px = mc.get(c, {}).get(prev_month(key))
                if px:
                    v += q * px
                else:
                    miss = True
            return v, miss

        st = self.db.get("last_state") or {}
        out, idx, peak, bidx = [], 1.0, 1.0, 1.0
        keys = [b.strftime("%Y-%m") for b in bounds]
        for i, key in enumerate(keys):
            e0, miss0 = value(key)
            if i + 1 < len(keys):
                e1, miss1 = value(keys[i + 1])
                end_ts = bounds[i + 1].timestamp()
            else:
                e1, miss1 = st.get("total", e0), False
                end_ts = now_ts()
            fl = [(f["ts"], f["sign"] * (f.get("krw") or 0)) for f in flows if bounds[i].timestamp() <= f["ts"] < end_ts]
            net = sum(k for _, k in fl)
            base = e0 + 0.5 * net
            pnl = e1 - e0 - net
            ret = pnl / base * 100 if base > 1000 else None
            if ret is not None:
                idx *= 1 + ret / 100
            peak = max(peak, idx)
            b0 = mc["BTC"].get(prev_month(key))
            b1 = mc["BTC"].get(key)
            bret = (b1 / b0 - 1) * 100 if b0 and b1 else None
            if bret is not None:
                bidx *= 1 + bret / 100
            out.append({"month": key, "e0": e0, "e1": e1, "dep": sum(k for _, k in fl if k > 0),
                        "wd": -sum(k for _, k in fl if k < 0), "pnl": pnl, "ret": ret, "cum": (idx - 1) * 100,
                        "dd": (idx / peak - 1) * 100, "btc": bret, "btc_cum": (bidx - 1) * 100,
                        "warn": "가격 없는 코인 포함" if (miss0 or miss1) else ""})
        return out

    def send_export(self):
        try:
            data, sm = self.build_export()
        except Exception as e:
            log(f"내보내기 실패: {type(e).__name__} {e}")
            send(f"⚠️ 매매 기록 파일을 만들다 오류가 났어요 ({esc(type(e).__name__)}).")
            return
        send_document(data, f"bithumb_records_{kst(now_ts()):%Y%m%d}.zip",
                      "매매 기록 (엑셀로 열 수 있는 파일 9개) · 금액이 들어 있으니 공유 주의")
        ms = [m for m in sm["months"] if m["ret"] is not None]
        lines = ["<b>손익 맞추기</b>",
                 boxq([f"평생 순수익 <b>{man(sm['net'], True)}원</b>",
                       f"① 실현손익 {man(sm['realized'], True)}원",
                       f"② 평가손익 {man(sm['unreal'], True)}원",
                       f"③ 상장폐지로 사라진 원가 {man(-sm['gone'], True)}원",
                       f"④ 기타 {man(sm['other'], True)}원"])]
        if sm["top_gone"]:
            lines.append("상장폐지 코인: " + ", ".join(f"{d['coin']} {man(-d['left_cost'], True)}" for d in sm["top_gone"]))
        if ms:
            worst = min(ms, key=lambda m: m["dd"])
            lines.append(f"가장 깊었던 낙폭: {worst['month']} 고점 대비 {worst['dd']:.0f}%")
        lines.append("<i>파일을 Claude 대화에 첨부하면 자세히 분석해 드려요</i>")
        send("\n".join(lines))

    # ── 불러올 수 있는 가장 오래된 기록 (날짜만) ──
    def oldest_record(self, path, params, ts_keys):
        def ts_of(x):
            return parse_ts(next((x.get(k) for k in ts_keys if x.get(k)), None))
        try:
            data = bget(path, {**params, "page": 1, "limit": 2, "order_by": "asc"})
            if isinstance(data, list) and not data:
                return None, "기록 없음"
            t = [ts_of(x) for x in data if isinstance(x, dict)] if isinstance(data, list) else []
            if len(t) == 1 and t[0]:
                return t[0], "ok"
            if len(t) == 2 and t[0] and t[1] and t[0] <= t[1]:      # 오래된 순 정렬이 실제로 적용된 경우만 믿음
                return t[0], "ok"
        except BithumbError as e:
            if e.status in (401, 403):
                return None, "권한 없음"
        oldest, page = None, 1                      # 오래된 순 정렬이 안 되면 최신순으로 끝까지 넘겨봄
        while page <= 300:
            try:
                data = bget(path, {**params, "page": page, "limit": 100, "order_by": "desc"})
            except BithumbError:
                break
            if not isinstance(data, list) or not data:
                break
            t = [ts_of(x) for x in data if isinstance(x, dict) and ts_of(x)]
            if t:
                oldest = min(t + ([oldest] if oldest else []))
            if len(data) < 100:
                break
            page += 1
        return oldest, ("ok" if oldest else "기록 없음") + (" (300쪽 이상 — 더 있을 수 있음)" if page > 300 else "")

    def block_history(self):
        rows = [("체결", "/v1/orders", {"states": ["done", "cancel"]}, ("created_at",)),
                ("원화 입금", "/v1/deposits/krw", {}, ("done_at", "created_at")),
                ("원화 출금", "/v1/withdraws/krw", {}, ("done_at", "created_at")),
                ("코인 입금", "/v1/deposits", {}, ("done_at", "created_at")),
                ("코인 출금", "/v1/withdraws", {}, ("done_at", "created_at"))]
        lines = ["<b>불러올 수 있는 가장 오래된 기록</b> <i>(날짜만)</i>"]
        first = None
        for name, path, params, keys in rows:
            ts, note = self.oldest_record(path, params, keys)
            if ts:
                first = min(first or ts, ts)
                lines.append(f"{name}: <b>{kst(ts):%Y-%m-%d}</b>" + ("" if note == "ok" else f" <i>{esc(note)}</i>"))
            else:
                lines.append(f"{name}: {esc(note)}")
        if first:
            yrs = (now_ts() - first) / 86400 / 365
            lines.append(f"\n약 {yrs:.1f}년치 기록을 불러올 수 있어요.")
        return "\n".join(lines)

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

    # ── 보유 코인 손실 점검 (같은 경고는 한 번만, 여러 개면 한 메시지로) ──
    def long_term_lines(self):
        """장기 모으기 코인과 포기선 (공개 accumulate_cfg.json)."""
        cache = self.db.get("lt_cache")
        if cache and now_ts() - cache.get("t", 0) < 6 * 3600:
            return cache["v"]
        v = (cache or {}).get("v", {})
        try:
            r = requests.get(f"https://raw.githubusercontent.com/{GH_REPO}/main/data/accumulate_cfg.json", timeout=10)
            cfg = r.json()
            v = {c: fnum((cfg.get("manual") or {}).get(c, {}).get("inval")) or None
                 for c in (cfg.get("longterm") or cfg.get("watch", []))}
            self.db.put("keep_forever", [str(x).upper() for x in cfg.get("keep_forever", [])])
        except Exception:
            pass
        self.db.put("lt_cache", {"t": now_ts(), "v": v})
        return v

    def _ma20(self, coin):
        c = self.db.get("ma20_cache", {}) or {}
        hit = c.get(coin)
        if hit and now_ts() - hit["t"] < 3600:
            return hit["v"]
        val = None
        try:
            data = bget("/v1/candles/days", {"market": f"KRW-{coin}", "count": 21}, private=False)
            closes = [fnum(r.get("trade_price")) for r in sorted(data, key=lambda r: r["candle_date_time_kst"])][:-1]
            if len(closes) >= 20:
                val = sum(closes[-20:]) / 20
        except (BithumbError, requests.RequestException, ValueError, KeyError, TypeError):
            pass
        c[coin] = {"t": now_ts(), "v": val}
        self.db.put("ma20_cache", c)
        return val

    def watch_losses(self, st):
        state = self.db.get("loss_state")
        first = state is None
        state = state or {}
        lt = self.long_term_lines()
        lines, deep_list = [], []
        keep = set(self.db.get("keep_forever", []) or [])
        for c, h in sorted(st["hold"].items(), key=lambda x: -x[1]["value"]):
            if c in keep or h["value"] < LOSS_MIN_KRW or not h["avg"] or h["pct"] is None:
                state.pop(c, None)                                    # 기념 코인(트론 등)은 알림 없이 그대로
                continue
            px, pc = h["price"], h["pct"]
            s0 = state.get(c) or {}
            if s0.get("avg") and abs(s0["avg"] / h["avg"] - 1) > 0.01:      # 추가 매수 등으로 평단이 바뀌면 처음부터
                s0 = {}
            s0["avg"] = h["avg"]
            name = f"<b>{c}</b>"
            if c in lt:                                                       # 장기 코인: 포기선 + 원금 회수 시점
                got = max([m for m in (100, 200, 500, 1000) if pc >= m] or [0])
                if got > (s0.get("milestone") or 0):
                    cost = h["qty"] * h["avg"]
                    sell_q = cost / px
                    lines.append(f"💎 {name} 장기 +{pc:.0f}% — 원금 회수 시점\n"
                                 f"   원금 {man(cost)}원만큼({qty(sell_q)}개) 팔면 나머지 {qty(h['qty'] - sell_q)}개는 공짜로 들고 가요")
                    s0["milestone"] = got
                inv = lt.get(c)
                if inv and px < inv and not s0.get("inval"):
                    lines.append(f"🔴 {name} 포기선 {price_txt(inv)} 아래 ({price_txt(px)}) — 장기 보유 이유가 깨졌는지 점검")
                    s0["inval"] = True
                elif inv and px > inv * 1.03:
                    s0["inval"] = False
                state[c] = s0
                continue
            if first and pc <= LOSS_CUT:                                      # 켜는 날 이미 깊이 물린 코인
                s0.update(level="deep", base=px)
                deep_list.append((c, h))
            elif s0.get("level") == "deep":
                base = s0.get("base") or px
                ma = self._ma20(c)
                if px >= base * 1.10:
                    lines.append(f"🟡 {name} 반등 +{(px / base - 1) * 100:.0f}% ({price_txt(base)} → {price_txt(px)}) · 평단 대비 {pct(pc)}\n"
                                 "   빠져나올 기회 — 정리하거나 줄일지 결정")
                    s0["base"] = px
                elif ma and px >= ma and not s0.get("ma_hit"):
                    lines.append(f"🟡 {name} 일봉 20일선 {price_txt(ma)} 도달 · 평단 대비 {pct(pc)}\n   빠져나올 기회 — 정리하거나 줄일지 결정")
                    s0["ma_hit"] = True
                elif px <= base * 0.90:
                    lines.append(f"🔴 {name} 물린 뒤 -{(1 - px / base) * 100:.0f}% 더 하락 ({price_txt(base)} → {price_txt(px)}) · 평단 대비 {pct(pc)}")
                    s0["base"] = px
                if pc > -3:
                    s0 = {"avg": h["avg"]}                                    # 거의 회복하면 일반으로
            else:
                lv = s0.get("level")
                if pc <= LOSS_CUT and lv != "cut":
                    lines.append(f"🔴 {name} {pct(pc)} · 정리 권고 (평단 {price_txt(h['avg'])} → {price_txt(px)})\n"
                                 f"   평가손실 {man(h['pnl'], True)}원 · 더 버티면 물리는 구간이에요")
                    s0.update(level="deep", base=px)
                elif pc <= LOSS_WARN and lv not in ("warn", "cut", "deep"):
                    cut_px = tick_round(h["avg"] * (1 + LOSS_CUT / 100), up=False)
                    lines.append(f"⚠️ {name} {pct(pc)} (평단 {price_txt(h['avg'])} → {price_txt(px)})\n"
                                 f"   평가손실 {man(h['pnl'], True)}원 · -8% {price_txt(cut_px)}원 아래면 정리 권고")
                    s0["level"] = "warn"
                elif pc > -3 and lv:
                    s0 = {"avg": h["avg"]}
            state[c] = s0
        for c in list(state):
            if c not in st["hold"] and not c.startswith("_"):
                state.pop(c)
        self.db.put("loss_state", state)
        if deep_list:
            tot_v = sum(h["value"] for _, h in deep_list)
            tot_l = sum(h["pnl"] or 0 for _, h in deep_list)
            rows = [f"{c:<7}{pct(h['pct'], 0):>6} {man(h['value']):>7} {man(h['pnl'], True):>8}" for c, h in deep_list]
            send("🔎 <b>물린 코인 점검</b> (한 번만 보내요)\n<i>코인 · 수익률 · 평가금액 · 평가손익</i>\n<pre>" + esc("\n".join(rows)) + "</pre>"
                 f"\n묶인 돈 {man(tot_v)}원 · 평가손실 {man(tot_l, True)}원\n\n"
                 "앞으로 이 코인들은 손실 경고 대신 <b>+10% 반등</b>이나 <b>20일선 도달</b> 때 '빠져나올 기회'로 알려 드려요.\n"
                 "<i>판단 기준: 지금 이 돈이 현금이라면 이 코인을 이 가격에 살까?</i>")
        if lines:
            send("<b>보유 코인 점검</b>\n" + "\n".join(lines))

    def public_levels(self):
        """공개 보유목록(positions.json)의 손절가·목표가."""
        cache = self.db.get("pub_levels_cache")
        if cache and now_ts() - cache.get("t", 0) < 1800:
            return cache["v"]
        try:
            r = requests.get(f"https://raw.githubusercontent.com/{GH_REPO}/main/data/positions.json", timeout=10)
            v = {p["ticker"]: {k: fnum(p[k]) for k in ("stop", "t1", "t2") if p.get(k)}
                 for p in r.json().get("positions", []) if p.get("ticker")}
        except Exception:
            v = (cache or {}).get("v", {})
        self.db.put("pub_levels_cache", {"t": now_ts(), "v": v})
        return v

    def public_stops(self):
        return {c: lv["stop"] for c, lv in (self.public_levels() or {}).items() if lv.get("stop")}

    @staticmethod
    def split_major(st):
        rows = sorted(st["hold"].items(), key=lambda x: -x[1]["value"])
        major = [(c, h) for c, h in rows if h["value"] >= MAJOR_KRW]
        minor = [(c, h) for c, h in rows if h["value"] < MAJOR_KRW]
        return major, minor

    @staticmethod
    def group_sum(items):
        val = sum(h["value"] for _, h in items)
        inv = sum(h["qty"] * h["avg"] for _, h in items if h["avg"])
        pnl = sum(h["pnl"] or 0 for _, h in items)
        return {"n": len(items), "value": val, "pnl": pnl, "pct": pnl / inv * 100 if inv else None}

    # ── 메시지 ──
    def head(self, st, title):
        t = kst(st["ts"])
        wd = "월화수목금토일"[t.weekday()]
        cash_w = st["cash"] / st["total"] * 100 if st["total"] else 0
        return (f"<b>{title}</b> · {t:%m/%d}({wd}) {t:%H:%M}\n"
                f"총자산 <b>{won(st['total'])}</b>\n"
                f"현금 {man(st['cash'])}원 ({cash_w:.0f}%)\n"
                f"코인 {len(st['hold'])}종목")

    def block_periods(self, curve, flows):
        b = self.bounds()
        rows = []
        for name, key in (("오늘", "day"), ("이번주", "week"), ("이번달", "month"), ("올해", "year")):
            p = self.period(b[key], curve, flows)
            if p:
                rows.append(f"{label(name)} {man(p['pnl'], True):>9} {pct(p['ret']):>7}")
        start = self.db.get("start_ts")
        note = f"\n<i>{kst(start):%m/%d} 기록 시작 · 입출금 제외</i>" if start else ""
        return "<b>기간별 손익</b>\n<pre>" + esc("\n".join(rows)) + "</pre>" + note

    def block_holdings(self, st):
        if not st["hold"]:
            return "<b>보유 코인</b>\n없음 (전부 현금)"
        major, minor = self.split_major(st)
        out = [f"<b>보유 코인</b> <i>{man(MAJOR_KRW)}원 이상</i>",
               "<i>비중 · 수익률 · 평가손익</i>"]
        if major:
            rows = [f"{dot(h['pct'])}{c:<6}{h['weight']:>4.0f}% {pct(h['pct']):>7} {man(h['pnl'], True):>8}"
                    for c, h in major]
            out.append("<pre>" + esc("\n".join(rows)) + "</pre>")
        else:
            out.append(f"{man(MAJOR_KRW)}원 이상 보유 코인 없음")
        if minor:
            m = self.group_sum(minor)
            out.append(f"기타 {m['n']}종목 {man(m['value'])}원 ({pct(m['pct'])})")
        allg = self.group_sum(list(st["hold"].items()))
        out.append(f"평가손익 합계 <b>{man(allg['pnl'], True)}원</b>")
        out.append(f"({pct(allg['pct'])})")
        return "\n".join(out)

    def block_holdings_detail(self, st):
        major, _ = self.split_major(st)
        levels = self.public_levels() or {}
        lines = []
        for c, h in major:
            ln = (f"{dot(h['pct'])} <b>{c}</b> {pct(h['pct'])} · 비중 {h['weight']:.0f}%\n"
                  f"   평단 {price_txt(h['avg'])} → 현재 {price_txt(h['price'])}\n"
                  f"   {qty(h['qty'])}개 · 평가 {won(h['value'])} ({man(h['pnl'], True)})")
            lv = levels.get(c) or {}
            marks = []
            for k, name in (("stop", "손절"), ("t1", "1차 목표"), ("t2", "2차 목표")):
                if lv.get(k):
                    marks.append(f"{name} {price_txt(lv[k])} ({pct((lv[k] / h['price'] - 1) * 100)})")
            if marks:
                ln += "".join(f"\n   {m}" for m in marks)
            lines.append(ln)
        return "\n".join(lines)

    # ── 계좌 점검 (상태 점수·할 일) ──
    def checkup(self, st, curve, flows):
        items, todos = [], []          # items: (상태 good/warn/bad/info, 문장)
        g = self.goal(curve, flows)
        total = st["total"] or 1
        if g and g["days"] >= 7:
            gap = g["gap"]
            s_ = "good" if gap >= 0 else ("warn" if gap >= -5 else "bad")
            items.append((s_, f"목표 페이스보다 {abs(gap):.1f}%p {'앞섬' if gap >= 0 else '뒤처짐'}"))
            if s_ == "bad":
                todos.append("목표보다 많이 뒤처짐 — 새 매수는 승률 높은 자리만 — 손실 종목부터 정리")
        elif g:
            items.append(("info", f"목표 페이스: 기록 {g['days']:.0f}일째 (7일부터 판정)"))
        cash_w = st["cash"] / total * 100
        if 10 <= cash_w <= 50:
            items.append(("good", f"현금 {cash_w:.0f}% (적정 10~50%)"))
        elif cash_w < 5:
            items.append(("bad", f"현금 {cash_w:.0f}% — 급락 때 살 돈이 없음"))
            todos.append("현금 10% 이상 확보 — 가장 약한 종목 일부 정리")
        elif cash_w < 10:
            items.append(("warn", f"현금 {cash_w:.0f}% — 조금 부족 (10% 이상 권장)"))
        else:
            items.append(("warn" if st["hold"] else "info", f"현금 {cash_w:.0f}% — 대기 중이면 OK"))
        if st["hold"]:
            c, h = max(st["hold"].items(), key=lambda x: x[1]["weight"])
            w = h["weight"]
            s_ = "good" if w <= 30 else ("warn" if w <= 40 else "bad")
            items.append((s_, f"최대 비중 {c} {w:.0f}%" + ("" if s_ == "good" else " — 한 종목 쏠림")))
            if s_ == "bad":
                todos.append(f"{c} 비중 {w:.0f}% → 40% 이하로 줄일지 검토")
        major, _ = self.split_major(st)
        levels = self.public_levels() or {}
        for c, h in major:
            stop = (levels.get(c) or {}).get("stop")
            if stop and h["price"] < stop:
                items.append(("bad", f"{c} 손절가 {price_txt(stop)} 아래 ({pct(h['pct'])})"))
                todos.append(f"{c} 손절가 이탈 — 정리할지 보유할지 결정")
            elif h["pct"] is not None and h["pct"] <= -10:
                items.append(("bad", f"{c} {pct(h['pct'])} — 큰 손실"))
                todos.append(f"{c} 손절 기준 다시 정하기 — 물타기는 계획 안에서만")
            elif h["pct"] is not None and h["pct"] <= -5:
                items.append(("warn", f"{c} {pct(h['pct'])} — 손절 기준 확인"))
            t1 = (levels.get(c) or {}).get("t1")
            if t1 and h["price"] >= t1:
                items.append(("good", f"{c} 1차 목표 {price_txt(t1)} 도달"))
                todos.append(f"{c} 1차 목표 도달 — 일부 익절 검토")
        sa = self.stats(0)
        if sa["n"] >= 5:
            pf = sa["pf"] if sa["pf"] != float("inf") else 9.9
            s_ = "good" if pf >= 1.5 else ("warn" if pf >= 1 else "bad")
            items.append((s_, f"매매 성적 승률 {sa['wr']:.0f}% · 손익비 {pf:.2f} ({sa['n']}건)"))
            if s_ == "bad":
                todos.append("잃는 매매가 더 큼 — 손절은 빨리, 익절은 조금 길게")
        else:
            items.append(("info", f"매매 성적: 매도 {sa['n']}건 (5건부터 판정)"))
        if curve:
            _, cdd = self.drawdown(curve)
            s_ = "good" if cdd > -5 else ("warn" if cdd > -10 else "bad")
            items.append((s_, f"고점 대비 {cdd:.1f}%"))
        bad = sum(1 for k, _ in items if k == "bad")
        warn = sum(1 for k, _ in items if k == "warn")
        score = max(0, 100 - 15 * bad - 6 * warn)
        grade = "양호" if score >= 80 else ("주의" if score >= 60 else "위험")
        return {"score": score, "grade": grade, "items": items, "todos": todos[:3]}

    def block_checkup(self, ck):
        icon = {"good": "🟢", "warn": "🟡", "bad": "🔴", "info": "⚪"}
        order = {"bad": 0, "warn": 1, "good": 2, "info": 3}
        lines = ["<b>계좌 점검</b>",
                 boxq([f"종합 <b>{ck['grade']}</b> · {ck['score']}/100", bar(ck["score"] / 100)])]
        lines += [f"{icon[k]} " + esc(wrap(t, 34, "     ")) for k, t in sorted(ck["items"], key=lambda x: order[x[0]])]
        if ck["todos"]:
            lines.append("\n<b>할 일</b>")
            lines += [f"{i}. " + esc(wrap(t, 34, "   ")) for i, t in enumerate(ck["todos"], 1)]
        else:
            lines.append("\n지금 급하게 손볼 것은 없어요.")
        return "\n".join(lines)

    def block_goal(self, g):
        if not g:
            return ""
        state = "🟢 앞서는 중" if g["gap"] >= 0 else "🔴 뒤처짐"
        prog = max(g["ret"], 0) / GOAL_PCT
        lines = [f"🎯 <b>연 {GOAL_PCT:.0f}% 목표</b> · {g['days']:.0f}일째",
                 boxq([f"지금까지 <b>{pct(g['ret'])}</b> ({man(g['pnl'], True)}원)",
                        f"{bar(prog)} {prog * 100:.0f}% 달성",
                        f"목표 페이스 {pct(g['target'])}",
                        f"{state} <b>{abs(g['gap']):.1f}%p</b>"]),
                 f"이번달 {pct(g['month'])}",
                 f"월 목표 {pct(g['month_goal'])}"]
        if g["need_m"] is not None:
            lines.append(f"{kst(g['end']):%m/%d}까지 매달 {pct(g['need_m'])} 필요")
        return "\n".join(lines)

    def block_perf(self, curve):
        b = self.bounds()
        sm, sa = self.stats(b["month"]), self.stats(0)
        mdd, cdd = self.drawdown(curve) if curve else (0, 0)

        def line(name, s):
            if not s["n"]:
                return f"{name}: 매도 기록 없음"
            return (f"{name} {s['n']}건 · 승률 {s['wr']:.0f}%\n"
                    f"   {s['w']}승 {s['l']}패 · {man(s['pnl'], True)}원")

        out = ["<b>매매 성적</b> (매도 체결 기준)", line("이번달", sm), line("누적", sa)]
        if sa["n"]:
            pf = "∞" if sa["pf"] == float("inf") else (f"{sa['pf']:.2f}" if sa["pf"] is not None else "-")
            out.append("")
            out.append(f"평균 이익 {pct(sa['avg_w'])}")
            out.append(f"평균 손실 {pct(sa['avg_l'])}")
            out.append(f"손익비(PF) {pf}")
            out.append(f"거래당 기대값 {pct(sa['exp'], 2)}")
            if sa["hold_d"]:
                out.append(f"평균 보유 {sa['hold_d']:.1f}일")
        out.append("")
        out.append(f"수수료 누적 {won(sa['fee'])}")
        out.append(f"최대 낙폭 {mdd:.1f}%")
        out.append(f"지금 고점 대비 {cdd:.1f}%")
        return "\n".join(out), sa

    # ── 실현손익 (판 것만 · 기간별) ──
    def block_realized(self, st=None):
        b = self.bounds()
        life = self.db.get("lifetime")
        periods = [("오늘", b["day"]), ("이번주", b["week"]), ("이번달", b["month"]), ("올해", b["year"]), ("전체", 0)]
        lines = ["<b>실현손익</b>", "<i>판 것만 · 매도금액 − 평단×수량 − 수수료</i>"]
        rows = []
        for name, t in periods:
            rr = self.realized_rows(t)
            n = len(rr)
            w = sum(1 for r in rr if r["pnl"] > 0)
            pnl = sum(r["pnl"] for r in rr)
            wr = f"{w / n * 100:.0f}%" if n else "-"
            rows.append(f"{label(name)} {man(pnl, True):>9} {n:>5}건 {wr:>4}")
        lines.append("<i>기간 · 실현손익 · 매도 수 · 승률</i>")
        lines.append("<pre>" + esc("\n".join(rows)) + "</pre>")
        month = self.realized_rows(b["month"])
        if month:
            best = max(month, key=lambda r: r["pnl"])
            worst = min(month, key=lambda r: r["pnl"])
            lines.append(f"이번달 최고 {best['coin']} {man(best['pnl'], True)}원 ({pct(best['pct'])})")
            lines.append(f"이번달 최저 {worst['coin']} {man(worst['pnl'], True)}원 ({pct(worst['pct'])})")
        st = st or self.db.get("last_state")
        if st and st.get("hold"):
            upnl = sum(h["pnl"] or 0 for h in st["hold"].values())
            lines.append("")
            lines.append(f"참고: 평가손익(들고 있는 것) {man(upnl, True)}원")
        if not life:
            lines.append("<i>전체 기록 계산 전이라 '전체'는 기록 시작 이후만이에요</i>")
        return "\n".join(lines)

    def line_realized(self):
        b = self.bounds()
        today = sum(r["pnl"] for r in self.realized_rows(b["day"]))
        month = sum(r["pnl"] for r in self.realized_rows(b["month"]))
        return f"<b>실현손익</b> 오늘 {man(today, True)}원 · 이번달 {man(month, True)}원"

    def block_trades(self, limit=10):
        rows = self.db.q("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
        if not rows:
            return "<b>최근 매도</b>\n아직 기록이 없어요"
        lines = []
        for r in rows:
            hd = f"{r['hold_h'] / 24:.1f}d" if r["hold_h"] else "  - "
            lines.append(f"{kst(r['ts']):%m/%d} {r['coin']:<6}{hd:>5} {pct(r['pct']):>7} {man(r['pnl'], True) if r['pnl'] is not None else '-':>8}")
        return "<b>최근 매도</b>  <i>날짜 · 코인 · 보유일 · 수익률 · 손익</i>\n<pre>" + esc("\n".join(lines)) + "</pre>"

    def block_orders(self):
        rows = self.db.get("open_orders_full", []) or []
        if not rows:
            return "<b>걸어둔 주문</b>\n없음"
        prices = fetch_prices({r["coin"] for r in rows})
        lines = []
        for r in sorted(rows, key=lambda r: (r["coin"], r["side"])):
            cur = prices.get(r["coin"])
            dist = (r["price"] / cur - 1) * 100 if cur else None
            side = "매수" if r["side"] == "bid" else "매도"
            lines.append(f"{'🟢' if r['side'] == 'bid' else '🔵'} {r['coin']} {side} {price_txt(r['price'])}원 × {qty(r['vol'])}"
                         f" ({won(r['price'] * r['vol'])})" + (f" · 현재가 대비 {pct(dist)}" if dist is not None else ""))
        return "<b>걸어둔 주문</b>\n" + "\n".join(lines)

    def full_report(self, st=None, title="내 계좌"):
        st = st or self.db.get("last_state") or self.snapshot()
        curve, flows = self.curve()
        g = self.goal(curve, flows)
        perf, sa = self.block_perf(curve)
        parts = [self.head(st, title), self.block_checkup(self.checkup(st, curve, flows)), self.block_ytd(st),
                 self.block_periods(curve, flows), self.line_realized(), self.block_holdings(st),
                 self.block_goal(g), perf]
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
        lines = [f"<b>{prev:%Y년 %m월} 성적표</b>",
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
        first = t.split()[0] if t.split() else ""
        cmd = next((c for c, _ in COMMANDS if first == c), None)
        if not cmd:
            cmd = next((c for c, ws in WORDS.items() if any(w in t for w in ws)), None)
        if cmd in (None, "help"):
            send("<b>계좌 비서</b> — 메뉴 버튼이나 한글로 물어보세요\n"
                 + "\n".join(f"/{c} {d}" for c, d in COMMANDS[:-1])
                 + "\n\n예) '계좌', '점검', '보유', '승률', '주문', '목표'\n체결·입금은 자동으로 알려드려요.")
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
            send_photo(chart_account(self, st, curve, flows))
            send(self.full_report(st))
        elif cmd == "check":
            send_photo(chart_account(self, st, curve, flows))
            send(self.head(st, "내 계좌") + "\n\n" + self.block_checkup(self.checkup(st, curve, flows)))
        elif cmd == "holdings":
            send_photo(chart_holdings(self, st))
            send(self.head(st, "보유 코인") + "\n\n" + self.block_holdings(st) + "\n\n" + self.block_holdings_detail(st))
        elif cmd == "perf":
            send(self.block_perf(curve)[0] + "\n\n" + self.block_trades(5))
        elif cmd == "trades":
            send(self.block_trades(15))
        elif cmd in ("lifetime", "years", "coins"):
            if not self.db.get("lifetime"):
                self.start_lifetime(notify=True)
                send(self.LIFE_WAIT)
            elif not self.db.get("lifetime").get("coins") and cmd == "coins":
                self.start_lifetime(notify=False)
                send("코인별 성적은 새로 계산해야 해요. 몇 분 뒤 다시 '코인별'이라고 보내 주세요.")
            else:
                self.send_life(cmd)
        elif cmd == "export":
            send("파일을 만드는 중이에요. 기록이 많으면 몇 분 걸려요.")
            threading.Thread(target=lambda: Bot(DB(self.db.path)).send_export(), daemon=True).start()
        elif cmd == "realized":
            send(self.block_realized(st))
        elif cmd == "history":
            send(self.block_history())
        elif cmd == "year":
            send(self.head(st, "올해 실질 수익") + "\n\n" + self.block_ytd(st) + "\n\n" + self.block_goal(self.goal(curve, flows)))
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
                tb = traceback.extract_tb(e.__traceback__)
                where = f"{tb[-1].name}:{tb[-1].lineno}" if tb else "?"
                log(f"명령 처리 오류: {type(e).__name__} @ {where}")
                traceback.print_exc()
                send(f"⚠️ 처리 중 오류가 났어요\n<code>{esc(type(e).__name__)}: {esc(str(e)[:120])} @ {esc(where)}</code>\n"
                     "이 메시지를 캡처해서 보내 주시면 고칠게요.")

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
        if not self.db.get("history_sent"):
            try:
                send(self.block_history())
                self.db.put("history_sent", True)
            except Exception as e:
                log(f"기록 범위 확인 실패: {type(e).__name__}")
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
        y = self.db.get("ytd")
        if (not y or y.get("y0") != self.bounds()["year"]
                or (d.hour >= 5 and kst(y.get("computed", 0)).strftime("%Y-%m-%d") != today)):
            if now_ts() - (self.db.get("ytd_fail_at") or 0) > 3600:
                try:
                    self.compute_ytd()
                except (BithumbError, requests.RequestException) as e:
                    log(f"올해 수익 계산 실패: {type(e).__name__} {e}")
                    self.db.put("ytd_fail_at", now_ts())
        if d.hour >= REPORT_HOUR and self.db.get("last_daily") != today:
            self.db.put("last_daily", today)
            st = self.db.get("last_state")
            if st:
                curve, flows = self.curve()
                send_photo(chart_account(self, st, curve, flows))
            send(self.full_report(st, title="오늘의 결산"))
        life = self.db.get("lifetime")
        started = self.db.get("lifetime_started") or 0
        stale_start = now_ts() - started > 3600              # 계산 중 재시작(업데이트)돼 끊긴 경우 다시
        need_real = bool(life) and not self.db.q("SELECT 1 FROM realized LIMIT 1")   # 실현손익 기록표 첫 생성
        if ((not life or "coins" not in life or need_real) and stale_start) or \
                (life and d.weekday() == 6 and d.hour >= 4 and now_ts() - life["computed"] > 86400 * 3):
            self.db.put("lifetime_started", now_ts())
            self.start_lifetime(notify=not life or "coins" not in life)
        if d.day == 1 and d.hour >= 9 and self.db.get("last_monthly") != f"{d:%Y-%m}":
            self.db.put("last_monthly", f"{d:%Y-%m}")
            txt = self.month_report()
            if txt:
                send_photo(chart_monthly(self))
                send(txt)


# ───────────────────────── 이미지 카드 (텔레그램 사진) ─────────────────────────
# 색: 한국 시장 관례대로 상승=빨강, 하락=파랑. 글자는 잉크색, 색은 막대/선에만.
C_BG, C_INK, C_INK2, C_MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
C_GRID, C_AXIS = "#e1e0d9", "#c3c2b7"
C_UP, C_DOWN, C_LINE = "#e34948", "#2a78d6", "#0b0b0b"
_FONT = {"ok": None}


def _plt():
    """matplotlib 준비 (없으면 None → 글자만 보냄)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception:
        return None
    if _FONT["ok"] is None:
        _FONT["ok"] = False
        for path in ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                     "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
            if os.path.exists(path):
                font_manager.fontManager.addfont(path)
                bold = path.replace("NanumGothic.ttf", "NanumGothicBold.ttf").replace("Regular", "Bold")
                if os.path.exists(bold):
                    font_manager.fontManager.addfont(bold)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
                _FONT["ok"] = True
                break
        plt.rcParams.update({"axes.unicode_minus": False, "figure.facecolor": C_BG, "axes.facecolor": C_BG,
                             "savefig.facecolor": C_BG, "text.color": C_INK, "axes.edgecolor": C_AXIS,
                             "xtick.color": C_MUTED, "ytick.color": C_MUTED, "axes.labelcolor": C_INK2})
    return plt


def _png(fig, plt):
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    return buf.getvalue()


def _clean_axes(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(C_AXIS)
    ax.tick_params(length=0, labelsize=8.5)
    ax.grid(axis="y", color=C_GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def chart_account(bot, st, curve, flows):
    """계좌 카드: 총자산 · 수익률 추이(목표선) · 큰 보유 코인 수익률."""
    plt = _plt()
    if plt is None or not st:
        return None
    try:
        from matplotlib.ticker import FuncFormatter
        g = bot.goal(curve, flows)
        major, minor = bot.split_major(st)
        nb = max(len(major), 1)
        fig = plt.figure(figsize=(7.2, 6.4 + 0.42 * nb))
        t = kst(st["ts"])
        fig.text(0.06, 0.965, f"내 계좌 · {t:%m/%d} {'월화수목금토일'[t.weekday()]} {t:%H:%M}",
                 fontsize=10, color=C_MUTED, va="top")
        fig.text(0.06, 0.93, won(st["total"]), fontsize=24, fontweight="bold", va="top")
        cash_w = st["cash"] / st["total"] * 100 if st["total"] else 0
        sub = f"현금 {cash_w:.0f}% · 코인 {len(st['hold'])}종목"
        if g:
            yl = "올해" if g["since"] == bot.bounds()["year"] else "기록 후"
            sub += f"   |   {yl} {pct(g['ret'])} · 목표 페이스 {pct(g['target'])}"
        fig.text(0.06, 0.865, sub, fontsize=10.5, color=C_INK2, va="top")

        h_line = 3.2 / (6.4 + 0.42 * nb)
        ax = fig.add_axes([0.08, 0.83 - h_line - 0.04, 0.86, h_line])
        start = curve[0][0] if curve else 0
        pts = [r for r in curve if r[0] >= start] if curve else []
        if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 3600 * 6:
            step = max(1, len(pts) // 400)
            pts = pts[::step] + ([pts[-1]] if pts[-1] is not pts[::step][-1] else [])
            base = pts[0][2]
            xs = [datetime.fromtimestamp(r[0], KST) for r in pts]
            ys = [(r[2] / base - 1) * 100 for r in pts]
            goal_y = [((1 + GOAL_PCT / 100) ** ((r[0] - pts[0][0]) / 86400 / 365) - 1) * 100 for r in pts]
            ax.plot(xs, goal_y, color=C_MUTED, linewidth=1.4, linestyle=(0, (4, 3)))
            ax.plot(xs, ys, color=C_LINE, linewidth=2)
            ax.axhline(0, color=C_AXIS, linewidth=0.8)
            ax.annotate(f"내 수익률 {pct(ys[-1])}", (xs[-1], ys[-1]), xytext=(-4, 8), textcoords="offset points",
                        ha="right", fontsize=9, color=C_INK, fontweight="bold")
            ax.annotate(f"연 {GOAL_PCT:.0f}% 목표선", (xs[-1], goal_y[-1]), xytext=(-4, -14),
                        textcoords="offset points", ha="right", fontsize=8.5, color=C_MUTED)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%" if v else "0%"))
            import matplotlib.dates as mdates
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d", tz=KST))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6, tz=KST))
            _clean_axes(ax)
        else:
            ax.axis("off")
            ax.text(0.5, 0.5, "수익률 그래프는 기록이 하루쯤 쌓이면 그려져요", ha="center", va="center",
                    fontsize=10.5, color=C_MUTED, transform=ax.transAxes)
        ax.set_title("수익률 추이 (기록 시작 후 · 입출금 제외)", loc="left", fontsize=11, fontweight="bold", pad=10)

        h_bar = 0.42 * nb / (6.4 + 0.42 * nb)
        bx = fig.add_axes([0.20, 0.07, 0.62, h_bar])
        bx.set_title(f"보유 코인 수익률 ({man(MAJOR_KRW)}원 이상)", loc="left", fontsize=11, fontweight="bold",
                     pad=10, x=-0.2)
        if major:
            names = [c for c, _ in major][::-1]
            vals = [h["pct"] or 0 for _, h in major][::-1]
            ws = [h["weight"] for _, h in major][::-1]
            lim = max(5, max(abs(v) for v in vals) * 1.15)
            bx.barh(names, vals, height=0.5, color=[C_UP if v >= 0 else C_DOWN for v in vals])
            bx.axvline(0, color=C_AXIS, linewidth=0.8)
            bx.set_xlim(-lim, lim)
            for y, (v, w) in enumerate(zip(vals, ws)):
                bx.text(lim * 1.02, y, f"{pct(v)}  ·  {w:.0f}%", va="center", fontsize=9, color=C_INK)
            for side in ("top", "right", "left", "bottom"):
                bx.spines[side].set_visible(False)
            bx.tick_params(length=0, labelsize=10.5, labelcolor=C_INK)
            bx.set_xticks([])
        else:
            bx.axis("off")
            bx.text(0, 0.5, f"{man(MAJOR_KRW)}원 이상 보유 코인 없음", fontsize=10, color=C_MUTED,
                    transform=bx.transAxes, va="center")
        if minor:
            m = bot.group_sum(minor)
            fig.text(0.06, 0.025, f"기타 {m['n']}종목 · 합계 {man(m['value'])}원 · {pct(m['pct'])}",
                     fontsize=9, color=C_MUTED)
        return _png(fig, plt)
    except Exception as e:
        log(f"계좌 카드 그리기 실패: {type(e).__name__}")
        return None


def chart_holdings(bot, st):
    """보유 카드: 큰 보유 코인 표 (평단·현재가·수익률·손익·손절/목표 거리)."""
    plt = _plt()
    if plt is None or not st or not st["hold"]:
        return None
    try:
        major, minor = bot.split_major(st)
        levels = bot.public_levels() or {}
        rows = len(major) + (1 if minor else 0)
        fig = plt.figure(figsize=(7.2, 1.25 + 0.62 * max(rows, 1)))
        H = fig.get_figheight()
        y = lambda inch: 1 - inch / H
        t = kst(st["ts"])
        fig.text(0.05, y(0.3), f"보유 코인 · {t:%m/%d %H:%M}", fontsize=13, fontweight="bold", va="center")
        fig.text(0.95, y(0.3), f"{man(MAJOR_KRW)}원 이상", fontsize=9, color=C_MUTED, va="center", ha="right")
        cols = [(0.05, "코인", "left"), (0.30, "비중", "right"), (0.47, "평단", "right"), (0.64, "현재가", "right"),
                (0.79, "수익률", "right"), (0.95, "평가손익", "right")]
        for x, name, ha in cols:
            fig.text(x, y(0.78), name, fontsize=9, color=C_MUTED, ha=ha, va="center")
        fig.add_artist(plt.Line2D([0.05, 0.95], [y(0.95), y(0.95)], color=C_AXIS, linewidth=0.8))
        yy = 1.3
        for c, h in major:
            col = C_UP if (h["pct"] or 0) >= 0 else C_DOWN
            fig.add_artist(plt.Rectangle((0.035, y(yy) - 0.18 / H), 0.006, 0.36 / H, color=col, transform=fig.transFigure))
            vals = [c, f"{h['weight']:.0f}%", price_txt(h["avg"]), price_txt(h["price"]), pct(h["pct"]),
                    man(h["pnl"], True)]
            for (x, _, ha), v in zip(cols, vals):
                bold = v == c or v == pct(h["pct"])
                fig.text(x, y(yy), v, fontsize=11 if bold else 10, fontweight="bold" if bold else "normal",
                         ha=ha, va="center", color=C_INK)
            lv = levels.get(c) or {}
            marks = [f"{name} {price_txt(lv[k])} ({pct((lv[k] / h['price'] - 1) * 100)})"
                     for k, name in (("stop", "손절"), ("t1", "1차"), ("t2", "2차")) if lv.get(k)]
            if marks:
                fig.text(0.30, y(yy + 0.26), "  ·  ".join(marks), fontsize=8, color=C_MUTED, va="center")
            yy += 0.62
        if not major:
            fig.text(0.05, y(yy), f"{man(MAJOR_KRW)}원 이상 보유 코인 없음", fontsize=10, color=C_MUTED, va="center")
            yy += 0.62
        if minor:
            m = bot.group_sum(minor)
            fig.text(0.05, y(yy), f"기타 {m['n']}종목", fontsize=10, color=C_INK2, va="center")
            fig.text(0.95, y(yy), f"합계 {man(m['value'])}원 · {pct(m['pct'])}", fontsize=10, color=C_INK2,
                     va="center", ha="right")
        return _png(fig, plt)
    except Exception as e:
        log(f"보유 카드 그리기 실패: {type(e).__name__}")
        return None


def chart_monthly(bot):
    """월별 수익률 막대 + 월 목표선."""
    plt = _plt()
    curve, _ = bot.curve()
    if plt is None or len(curve) < 2:
        return None
    try:
        months, last = [], None
        for ts, _, idx in curve:
            key = f"{kst(ts):%Y-%m}"
            if not months or months[-1][0] != key:
                months.append([key, last if last else idx, idx])
            months[-1][2] = idx
            last = idx
        months = months[-12:]
        labels = [f"{int(k[5:])}월" for k, _, _ in months]
        vals = [(e / b - 1) * 100 if b else 0 for _, b, e in months]
        mg = ((1 + GOAL_PCT / 100) ** (1 / 12) - 1) * 100
        fig, ax = plt.subplots(figsize=(7.2, 4.6))
        fig.subplots_adjust(top=0.78, left=0.09, right=0.97, bottom=0.1)
        fig.text(0.05, 0.94, "월별 수익률", fontsize=14, fontweight="bold", va="top")
        hit = sum(1 for v in vals if v >= mg)
        fig.text(0.05, 0.86, f"월 목표 {pct(mg)} 달성 {hit}/{len(vals)}개월 · 점선 = 월 목표", fontsize=9.5,
                 color=C_INK2, va="top")
        ax.bar(labels, vals, width=0.55, color=[C_UP if v >= 0 else C_DOWN for v in vals])
        ax.axhline(mg, color=C_MUTED, linewidth=1.2, linestyle=(0, (4, 3)))
        ax.axhline(0, color=C_AXIS, linewidth=0.8)
        for i, v in enumerate(vals):
            ax.text(i, v + (0.3 if v >= 0 else -0.3), pct(v), ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=9, color=C_INK)
        _clean_axes(ax)
        ax.set_yticks([])
        lo, hi = min(vals + [0, mg]), max(vals + [0, mg])
        pad = max((hi - lo) * 0.18, 1)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlim(-0.6, max(len(vals), 6) - 0.4)
        return _png(fig, plt)
    except Exception as e:
        log(f"월간 카드 그리기 실패: {type(e).__name__}")
        return None


def chart_years(r):
    """연도별 수익률 막대 + 평생 순수익 헤더."""
    plt = _plt()
    if plt is None or not r or not r.get("years"):
        return None
    try:
        ys = r["years"]
        labels = [str(y["year"]) for y in ys]
        vals = [y["ret"] or 0 for y in ys]
        fig, ax = plt.subplots(figsize=(7.2, 4.9))
        fig.subplots_adjust(top=0.72, left=0.06, right=0.97, bottom=0.1)
        good = [y for y in ys if y["ret"] is not None]
        hit = sum(1 for y in good if y["ret"] >= GOAL_PCT)
        fig.text(0.05, 0.94, "연도별 수익률", fontsize=15, fontweight="bold", va="top")
        sub = f"{ys[0]['year']}~{ys[-1]['year']} · 연 {GOAL_PCT:.0f}% 달성 {hit}/{len(good)}년 · 입출금 제외"
        fig.text(0.05, 0.86, sub, fontsize=10, color=C_INK2, va="top")
        ax.bar(labels, vals, width=0.6, color=[C_UP if v >= 0 else C_DOWN for v in vals])
        ax.axhline(0, color=C_AXIS, linewidth=0.8)
        ax.axhline(GOAL_PCT, color=C_MUTED, linewidth=1.1, linestyle=(0, (4, 3)))
        ax.text(-0.45, GOAL_PCT, f"연 {GOAL_PCT:.0f}% 목표", ha="left", va="bottom", fontsize=8.5, color=C_MUTED)
        span = (max(vals + [0, GOAL_PCT]) - min(vals + [0])) or 1
        for i, (v, y) in enumerate(zip(vals, ys)):
            txt = pct(v, 0) + ("*" if y["warn"] else "")
            ax.text(i, v + (span * 0.02 if v >= 0 else -span * 0.02), txt, ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8.5, color=C_INK)
        _clean_axes(ax)
        ax.set_yticks([])
        lo, hi = min(vals + [0]), max(vals + [0, GOAL_PCT])
        ax.set_ylim(lo - span * 0.15, hi + span * 0.15)
        ax.set_xlim(-0.6, max(len(vals), 6) - 0.4)
        ax.spines["bottom"].set_visible(False)
        ax.tick_params(axis="x", labelsize=9)
        return _png(fig, plt)
    except Exception as e:
        log(f"연도별 카드 그리기 실패: {type(e).__name__}")
        return None


def chart_lifetime(r):
    """평생 순수익 카드: 큰 숫자 + 입금·출금·지금 자산 흐름."""
    plt = _plt()
    if plt is None or not r:
        return None
    try:
        net_in = r["dep"] - r["wd"]
        fig = plt.figure(figsize=(7.2, 4.0))
        fig.text(0.06, 0.90, f"평생 순수익 · {kst(r['first']):%Y.%m} ~ 지금", fontsize=11, color=C_MUTED, va="top")
        fig.text(0.06, 0.80, f"{man(r['pnl'], True)}원", fontsize=34, fontweight="bold", va="top",
                 color=C_UP if r["pnl"] >= 0 else C_DOWN)
        if net_in > 0:
            fig.text(0.06, 0.58, f"순투입 대비 {pct(r['pnl'] / net_in * 100)}", fontsize=13, color=C_INK, va="top")
        fig.add_artist(plt.Line2D([0.06, 0.94], [0.44, 0.44], color=C_GRID, linewidth=1))
        cols = [("총 입금", r["dep"]), ("총 출금", r["wd"]), ("순투입", net_in), ("지금 자산", r["now"])]
        for i, (name, v) in enumerate(cols):
            x = 0.06 + i * 0.225
            fig.text(x, 0.36, name, fontsize=9.5, color=C_MUTED, va="top")
            fig.text(x, 0.27, f"{man(v)}원", fontsize=13, fontweight="bold", va="top")
        fig.text(0.06, 0.08, f"매매 {r['n_orders']:,}건 · 입출금 {r['n_flows']:,}건 기준", fontsize=8.5, color=C_MUTED)
        return _png(fig, plt)
    except Exception as e:
        log(f"평생 카드 그리기 실패: {type(e).__name__}")
        return None


def chart_coins(r, top=8):
    """코인별 평생 손익: 번 코인 위, 잃은 코인 아래 (가로 막대)."""
    plt = _plt()
    if plt is None or not r or not r.get("coins"):
        return None
    try:
        rows = [c for c in r["coins"] if c["n"] or c["held"]]
        win = [c for c in rows if c["total"] > 0][:top]
        lose = sorted([c for c in rows if c["total"] < 0], key=lambda c: c["total"])[:top][::-1]
        sel = win + lose
        if not sel:
            return None
        n = len(sel)
        fig = plt.figure(figsize=(7.2, 1.5 + 0.38 * n))
        H = fig.get_figheight()
        fig.text(0.05, 1 - 0.35 / H, "코인별 평생 손익", fontsize=15, fontweight="bold", va="center")
        fig.text(0.05, 1 - 0.72 / H, f"번 코인 TOP {len(win)} · 잃은 코인 TOP {len(lose)} · 실현+평가 · • 보유 중",
                 fontsize=9.5, color=C_INK2, va="center")
        ax = fig.add_axes([0.17, 0.3 / H, 0.56, (H - 1.3) / H])
        names = [("• " if c["held"] else "") + c["coin"] for c in sel][::-1]
        vals = [c["total"] for c in sel][::-1]
        ax.barh(range(n), vals, height=0.55, color=[C_UP if v >= 0 else C_DOWN for v in vals])
        ax.axvline(0, color=C_AXIS, linewidth=0.8)
        ax.set_yticks(range(n))
        ax.set_yticklabels(names)
        lim = max(abs(v) for v in vals) * 1.05 or 1
        ax.set_xlim(-lim, lim)
        ax.set_ylim(-0.6, n - 0.4)
        for i, (v, c) in enumerate(zip(vals, sel[::-1])):
            wr = f" · 승률 {c['wr']:.0f}%" if c["wr"] is not None else ""
            ax.text(lim * 1.04, i, f"{man(v, True)}{wr}", va="center", fontsize=9, color=C_INK)
        for side in ("top", "right", "left", "bottom"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, labelsize=10, labelcolor=C_INK)
        ax.set_xticks([])
        return _png(fig, plt)
    except Exception as e:
        log(f"코인별 카드 그리기 실패: {type(e).__name__}")
        return None


def send_document(data: bytes, filename: str, caption=""):
    if not (TG_TOKEN and TG_CHAT):
        print(f"[document] {filename} {len(data)} bytes (텔레그램 설정 없음)")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                      data={"chat_id": TG_CHAT, "caption": caption},
                      files={"document": (filename, data, "application/zip")}, timeout=60)
    except Exception as e:
        log(f"텔레그램 파일 전송 오류: {type(e).__name__}")


def send_photo(png, caption=""):
    if not png:
        return
    if not (TG_TOKEN and TG_CHAT):
        print(f"[photo] {len(png)} bytes (텔레그램 설정 없음)")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                      data={"chat_id": TG_CHAT, "caption": caption}, files={"photo": ("card.png", png, "image/png")},
                      timeout=30)
    except Exception as e:
        log(f"텔레그램 사진 전송 오류: {type(e).__name__}")


def check():
    """설치 확인: 금액은 출력하지 않습니다."""
    ok = True
    if not (AK and SK):
        print("FAIL no Bithumb keys")
        return False
    tests = [("accounts", "/v1/accounts", None), ("orders", "/v1/orders", {"state": "wait", "limit": 1}),
             ("deposits", "/v1/deposits/krw", {"limit": 1}), ("withdraws", "/v1/withdraws/krw", {"limit": 1})]
    for name, path, params in tests:
        try:
            data = bget(path, params)
            extra = ""
            if path == "/v1/accounts":
                extra = f" ({sum(1 for a in data if a.get('currency') != 'KRW')} coins)"
            print(f"OK   {name}{extra}")
        except BithumbError as e:
            print(f"FAIL {name}: {e}")
            if name in ("accounts", "orders"):
                ok = False
    if TG_TOKEN and TG_CHAT:
        r = tg("sendMessage", chat_id=TG_CHAT, text="계좌 비서 설치 확인 메시지입니다.")
        print("OK   telegram" if r and r.get("ok") else "FAIL telegram (check token / chat id)")
    else:
        print("WARN no telegram settings")
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
