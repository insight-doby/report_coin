#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
코주부 시그널 엔진 v5 — 2~5일 스윙 / 빗썸 원화마켓

설계 원칙
  1) 진입가·손절·목표는 AI가 아니라 '코드가 캔들에서 계산'한다.
  2) AI(Gemini)는 악재 검증(상폐·언락·해킹·규제)만 한다. 가격은 만들지 않는다.
  3) 모든 추천은 장부(ledger)에 '고정된 계획'으로 기록되고 1시간봉 고가·저가로 채점된다.
     같은 캔들에서 손절과 목표가 동시에 닿으면 손절 우선(보수적), 수수료 반영.
  4) 시장 신호등(GO / SELECTIVE / STOP)이 STOP이면 매수 추천을 내지 않는다.

사용법
  python bot.py scan                 스캔 + 추천 + 장부 갱신 + 슬랙 (기본)
  python bot.py track                장부만 갱신 (1시간마다, AI 호출 없음)
  python bot.py backtest [티커 ...]  일봉 기반 과거 검증 (생존 편향 주의)
  python bot.py pumpdip [일수]       급등(+30%↑) 후 눌림 매수 연구 (기본 730일)

저장 위치
  LOCAL_DATA_DIR 가 있으면 그 폴더(워크플로가 커밋), 없으면 GitHub API(data/ 폴더).
"""
import os
import sys
import re
import json
import math
import time
import base64
import statistics
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import requests

# ==========================================================
# 1. 설정
# ==========================================================
KST = timezone(timedelta(hours=9))
ENV = os.environ.get

SLACK_BOT_TOKEN = ENV("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = ENV("SLACK_CHANNEL_ID", "")
GEMINI_API_KEY = ENV("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = ENV("GEMINI_MODEL", "gemini-2.5-flash")
GITHUB_TOKEN = ENV("GITHUB_TOKEN_BOT", "").strip()
GITHUB_REPO = ENV("GITHUB_REPO", "insight-doby/report_coin")
LOCAL_DATA_DIR = ENV("LOCAL_DATA_DIR", "")
DASHBOARD_URL = ENV("DASHBOARD_URL", "https://insight-doby.github.io/report_coin/")
WATCHLIST = [t.strip().upper() for t in ENV("WATCHLIST", "").split(",") if t.strip()]

BITHUMB = "https://api.bithumb.com/public"

UNIVERSE_N = int(ENV("UNIVERSE_N", "0"))              # 0이면 제한 없음(거래대금 하한만 적용)
MIN_VALUE_KRW = float(ENV("MIN_VALUE_KRW", "3e8"))     # 24h 거래대금 최소 3억원 (너무 얇으면 원하는 가격에 못 산다)
FEE_PCT_PER_SIDE = float(ENV("FEE_PCT_PER_SIDE", "0.04"))  # 빗썸 수수료 편도 0.04%

MIN_STOP_PCT = 2.5     # 손절폭 하한 (너무 타이트하면 노이즈에 털림)
MAX_STOP_PCT = 8.0     # 손절폭 상한 (2~5일 스윙에 비해 넓으면 제외)
T1_R, T2_R = 1.5, 2.5  # 목표 = 진입 + R배수 × 위험폭
T1_SELL_FRACTION = 0.5
MAX_HOLD_HOURS = 120   # 체결 후 5일 지나면 종가 청산(타임스톱)
WAIT_VALID_HOURS = 36  # 지정가 대기 추천 유효시간
COOLDOWN_HOURS = 48    # 손절 난 종목 재추천 금지

PICK_CAP = {"GO": 8, "SELECTIVE": 6, "STOP": 0, "UNKNOWN": 0}
MIN_SCORE = {"GO": 50, "SELECTIVE": 55}
SIZE_MULT = {"GO": 1.0, "SELECTIVE": 0.5, "STOP": 0.0, "UNKNOWN": 0.0}
GATE_LABEL = {"GO": "매수 가능", "SELECTIVE": "선별 매수", "STOP": "매수 금지", "UNKNOWN": "판단 보류"}

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

STABLES = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "FRAX", "UST", "LUSD",
           "SUSD", "USDD", "PYUSD", "FDUSD", "USDS", "EURT", "EURC", "USDE", "WBTC", "WETH"}

SETUP_LABEL = {"pullback": "눌림목", "breakout_retest": "돌파 후 안착", "squeeze": "수렴 돌파 대기", "div4h": "4시간 다이버전스"}


def log(*a):
    print(*a, flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_kst(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).isoformat(timespec="seconds")


def fnum(x, default=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def rp(v: float) -> float:
    """원화 가격을 가격대에 맞는 정밀도로 반올림."""
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


def fmt_krw(v) -> str:
    v = fnum(v, 0.0)
    if v >= 1000:
        return f"{int(round(v)):,}원"
    if v >= 1:
        dec = 1 if v >= 100 else 2
        return f"{v:.{dec}f}".rstrip("0").rstrip(".") + "원"
    if v <= 0:
        return "0원"
    dec = min(10, max(2, 3 - int(math.floor(math.log10(v)))))   # 유효숫자 4자리
    return f"{v:.{dec}f}".rstrip("0").rstrip(".") + "원"


def sanitize(o):
    """NaN/inf/numpy 타입을 JSON 안전한 값으로."""
    if isinstance(o, dict):
        return {str(k): sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    if isinstance(o, np.ndarray):
        return sanitize(o.tolist())
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if (math.isnan(v) or math.isinf(v)) else v
    return o


# ==========================================================
# 2. 저장소 (로컬 폴더 또는 GitHub API)
# ==========================================================
class Store:
    def __init__(self):
        self._sha = {}

    def _gh_headers(self):
        return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    def read(self, name: str, default):
        if LOCAL_DATA_DIR:
            p = os.path.join(LOCAL_DATA_DIR, name)
            if not os.path.exists(p):
                return default
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log(f"  ⚠️ {name} 읽기 실패: {e}")
                return default
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/{name}"
        try:
            r = requests.get(url, headers=self._gh_headers(), timeout=15)
            if r.status_code == 200:
                info = r.json()
                self._sha[name] = info["sha"]
                return json.loads(base64.b64decode(info["content"]).decode("utf-8"))
            if r.status_code != 404:
                log(f"  ⚠️ GitHub 읽기 {name}: HTTP {r.status_code}")
        except Exception as e:
            log(f"  ⚠️ GitHub 읽기 {name} 실패: {e}")
        return default

    def write(self, name: str, obj, msg: str = "update") -> bool:
        body = json.dumps(sanitize(obj), ensure_ascii=False, indent=1, allow_nan=False)
        if LOCAL_DATA_DIR:
            os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
            with open(os.path.join(LOCAL_DATA_DIR, name), "w", encoding="utf-8") as f:
                f.write(body)
            return True
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/{name}"
        payload = {"message": msg, "content": base64.b64encode(body.encode("utf-8")).decode()}
        sha = self._sha.get(name)
        if sha is None:
            try:
                r = requests.get(url, headers=self._gh_headers(), timeout=15)
                if r.status_code == 200:
                    sha = r.json().get("sha")
            except Exception:
                pass
        if sha:
            payload["sha"] = sha
        try:
            r = requests.put(url, headers=self._gh_headers(), json=payload, timeout=30)
            if r.status_code in (200, 201):
                self._sha[name] = r.json().get("content", {}).get("sha")
                return True
            log(f"  ❌ GitHub 쓰기 {name}: HTTP {r.status_code} {r.text[:120]}")
        except Exception as e:
            log(f"  ❌ GitHub 쓰기 {name} 실패: {e}")
        return False


# ==========================================================
# 3. 네트워크 / 빗썸 데이터
# ==========================================================
_session = requests.Session()


def http_json(url: str, params=None, timeout: int = 10, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 500, 502, 503):
                time.sleep(1.5 * (i + 1))
                continue
            return None
        except Exception as e:
            last = str(e)[:100]
            time.sleep(1.5 * (i + 1))
    log(f"  ⚠️ GET 실패 {url.split('?')[0]}: {last}")
    return None


def fetch_tickers_all() -> Optional[dict]:
    j = http_json(f"{BITHUMB}/ticker/ALL_KRW")
    if not j or j.get("status") != "0000":
        return None
    out = {}
    for tk, info in (j.get("data") or {}).items():
        if tk == "date" or not isinstance(info, dict):
            continue
        price = fnum(info.get("closing_price"))
        if not price or price <= 0:
            continue
        out[tk.upper()] = {
            "price": price,
            "chg": fnum(info.get("fluctate_rate_24H"), 0.0),
            "value": fnum(info.get("acc_trade_value_24H"), 0.0),
        }
    return out


def fetch_candles(ticker: str, interval: str, limit: Optional[int] = 300) -> Optional[dict]:
    """빗썸 캔들 → dict(t,o,h,l,c,v). 응답 행: [시각ms, 시가, 종가, 고가, 저가, 거래량]."""
    j = http_json(f"{BITHUMB}/candlestick/{ticker}_KRW/{interval}")
    if not j or j.get("status") != "0000":
        return None
    rows = []
    for r in j.get("data") or []:
        if not isinstance(r, list) or len(r) < 6:
            continue
        try:
            t, o, c, h, l, v = (float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        except (TypeError, ValueError):
            continue
        if h < l or c <= 0 or not (l * 0.999 <= c <= h * 1.001):   # 컬럼 순서/이상값 방어
            continue
        rows.append((t, o, h, l, c, v))
    if len(rows) < 20:
        return None
    rows.sort(key=lambda x: x[0])
    if limit:
        rows = rows[-limit:]
    a = np.array(rows, dtype=float)
    return {"t": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def slice_completed(cd: dict, interval_ms: int, at_ms: float) -> dict:
    """진행 중인 마지막 봉을 제외한 완성봉만."""
    n = len(cd["t"])
    if n and cd["t"][-1] + interval_ms > at_ms:
        n -= 1
    return {k: v[:n] for k, v in cd.items()}


KR_NAMES = {}


def fetch_korean_names(store=None) -> dict:
    """티커→한글명. 빗썸 종목목록 → 실패 시 업비트 → 그래도 실패면 지난번 저장본."""
    names = {}
    for url in ("https://api.bithumb.com/v1/market/all?isDetails=false", "https://api.upbit.com/v1/market/all"):
        j = http_json(url, retries=2)
        if isinstance(j, list):
            for m in j:
                mk = str(m.get("market", ""))
                if mk.startswith("KRW-") and m.get("korean_name"):
                    names.setdefault(mk[4:].upper(), str(m["korean_name"]).strip())
        if len(names) > 50:
            break
    if store is not None:
        if names:
            store.write("names.json", names, "한글명")
        else:
            names = store.read("names.json", {}) or {}
    KR_NAMES.clear()
    KR_NAMES.update(names)
    return names


def kname(tk: str) -> str:
    return KR_NAMES.get(str(tk).upper(), "")


def label(tk: str) -> str:
    """'비트코인(BTC)' — 한글명이 없으면 티커만."""
    n = kname(tk)
    return f"{n}({tk})" if n else str(tk)


def fetch_taker_ratio() -> Optional[float]:
    """바이낸스 BTC 선물 최근 4시간 테이커 매수비율 (실패해도 무방)."""
    j = http_json("https://fapi.binance.com/futures/data/takerlongshortRatio",
                  params={"symbol": "BTCUSDT", "period": "1h", "limit": 4}, retries=1, timeout=8)
    try:
        vals = [float(r["buyRatio"]) for r in j]
        return round(sum(vals) / len(vals), 3) if vals else None
    except Exception:
        return None


# ==========================================================
# 4. 지표
# ==========================================================
def ema(arr, period: int) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    out = np.full(len(a), np.nan)
    if len(a) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = a[:period].mean()
    for i in range(period, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes, period: int = 14) -> Optional[float]:
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return None
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    ls = np.where(d < 0, -d, 0.0)
    ag, al = g[:period].mean(), ls[:period].mean()
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + ls[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return float(100 - 100 / (1 + ag / al))


def atr(h, l, c, period: int = 14) -> Optional[float]:
    h, l, c = (np.asarray(x, dtype=float) for x in (h, l, c))
    if len(c) < period + 1:
        return None
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    a = tr[:period].mean()
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return float(a)


def pivot_lows(l, k: int = 3) -> list:
    out = []
    for i in range(k, len(l) - k):
        if l[i] == min(l[i - k:i + k + 1]):
            out.append(i)
    return out


def h1_stabilized(h1: Optional[dict]) -> Optional[bool]:
    """1시간봉 반등 확인: 직전 마감봉이 양봉이거나, RSI 상승 + 5EMA 위. 데이터 없으면 None(미확인)."""
    if not h1 or len(h1["c"]) < 20:
        return None
    c, o = h1["c"], h1["o"]
    bullish = bool(c[-1] > o[-1])
    r_now, r_prev = rsi(c), rsi(c[:-2])
    rising = r_now is not None and r_prev is not None and r_now > r_prev
    above5 = bool(c[-1] > ema(c, 5)[-1])
    return bool(bullish or (rising and above5))


# ==========================================================
# 5. 시장 신호등
# ==========================================================
def compute_market(ctxs: dict, taker_ratio: Optional[float]) -> dict:
    out = {"gate": "UNKNOWN", "label": GATE_LABEL["UNKNOWN"], "size_mult": 0.0, "checks": [], "metrics": {}, "headline": ""}
    btc = ctxs.get("BTC")
    if not btc or len(btc["d"]["c"]) < 70:
        out["checks"].append({"text": "BTC 일봉 데이터 부족 — 안전을 위해 매수 보류", "ok": False})
        out["headline"] = "BTC 데이터를 못 가져와 판단을 보류합니다."
        return out

    c = btc["d"]["c"]
    cur = btc["cur"]
    e20, e60 = ema(c, 20), ema(c, 60)
    chg = btc["chg24"]
    above20 = cur > e20[-1]
    rising = e20[-1] > e20[-6]
    vs20 = (cur / e20[-1] - 1) * 100
    ret7 = (cur / c[-7] - 1) * 100 if len(c) >= 7 else 0.0

    above, total, adv = 0, 0, 0
    for tk, cx in ctxs.items():
        if tk == "BTC" or len(cx["d"]["c"]) < 25:
            continue
        e = ema(cx["d"]["c"], 20)[-1]
        if np.isnan(e):
            continue
        total += 1
        above += int(cx["cur"] > e)
        adv += int(cx["chg24"] > 0)
    breadth = round(above / total * 100, 1) if total else None
    adv_share = round(adv / total * 100, 1) if total else None

    vol_vs_avg = None
    v_all = btc["d"]["v"] * btc["d"]["c"]
    if len(v_all) >= 30 and btc.get("value24"):
        base = float(np.mean(v_all[-30:]))
        vol_vs_avg = round(btc["value24"] / base * 100, 1) if base > 0 else None

    overheat = chg >= 6 or vs20 >= 12
    checks = [
        {"text": f"BTC가 일봉 20일선 {'위' if above20 else '아래'} ({vs20:+.1f}%)", "ok": bool(above20)},
        {"text": f"20일선 {'우상향' if rising else '하락 중'}", "ok": bool(rising)},
    ]
    if breadth is not None:
        checks.append({"text": f"알트 {breadth:.0f}%가 20일선 위 (기준 50%)", "ok": breadth >= 50})
    checks.append({"text": f"BTC 24시간 {chg:+.1f}%", "ok": chg > -2.5})
    if taker_ratio is not None:
        checks.append({"text": f"BTC 선물 매수체결 {taker_ratio:.0%} ({'매수 우세' if taker_ratio >= 0.5 else '매도 우세'})",
                       "ok": taker_ratio >= 0.47})
    checks.append({"text": "과열 아님" if not overheat else f"과열 구간 (24h {chg:+.1f}%, 20일선 이격 {vs20:+.1f}%)",
                   "ok": not overheat})

    hard = chg <= -4 or (taker_ratio is not None and taker_ratio < 0.42 and chg <= -2)
    weak = (not above20) and (not rising) and (breadth is not None and breadth < 35)
    if hard:
        gate, head = "STOP", "BTC가 급락하거나 매도가 우세합니다. 오늘은 신규 매수를 쉬세요."
    elif weak:
        gate, head = "STOP", "BTC 하락 추세에 알트 대부분이 약세입니다. 신규 매수를 쉬세요."
    elif above20 and breadth is not None and breadth >= 50 and chg > -2.5 and not overheat:
        gate, head = "GO", "추세가 살아 있고 시장 폭도 넓습니다. 추천 구간에서 계획대로 매수 가능."
    elif overheat:
        gate, head = "SELECTIVE", "과열 구간입니다. 추격은 금지하고 눌림 구간만 소액으로."
    else:
        gate, head = "SELECTIVE", "조건이 일부만 맞습니다. 확실한 자리만 평소의 절반 비중으로."

    out.update({
        "gate": gate, "label": GATE_LABEL[gate], "size_mult": SIZE_MULT[gate], "checks": checks, "headline": head,
        "metrics": {"btc_price": cur, "btc_chg_24h": chg, "btc_vs_ema20_pct": round(vs20, 2), "btc_7d_pct": round(ret7, 2),
                    "breadth_pct": breadth, "advancers_pct": adv_share, "taker_buy_ratio": taker_ratio,
                    "btc_volume_vs_30d_pct": vol_vs_avg},
    })
    return out


# ==========================================================
# 6. 셋업 탐지 (코드가 가격을 계산한다)
# ==========================================================
MIN_BARS = 130


def make_plan(entry: float, stop_raw: float, state: str, zone_lo: float, zone_hi: float, max_stop: float = None) -> Optional[dict]:
    """진입·손절·목표 확정. 손절폭이 범위를 벗어나면 None."""
    if entry <= 0 or stop_raw <= 0 or stop_raw >= entry:
        return None
    stop = stop_raw
    min_dist = entry * MIN_STOP_PCT / 100
    if entry - stop < min_dist:
        stop = entry - min_dist
    risk = entry - stop
    stop_pct = risk / entry * 100
    if stop_pct > (max_stop or MAX_STOP_PCT):
        return None
    t1, t2 = entry + T1_R * risk, entry + T2_R * risk
    return {
        "state": state, "entry": rp(entry), "zone_lo": rp(zone_lo), "zone_hi": rp(zone_hi),
        "stop": rp(stop), "t1": rp(t1), "t2": rp(t2),
        "stop_pct": round(stop_pct, 2), "t1_pct": round((t1 / entry - 1) * 100, 2), "t2_pct": round((t2 / entry - 1) * 100, 2),
        "rr1": T1_R, "rr2": T2_R,
    }


def _liq_pts(value24: float) -> int:
    return 8 if value24 >= 2e10 else 4 if value24 >= 5e9 else 0


def _rs_pts(rs: Optional[float]) -> int:
    if rs is None:
        return 0
    return 18 if rs > 5 else 10 if rs > 0 else -4


def _state_for_zone(cur: float, zone_lo: float, zone_hi: float, max_gap: float = 0.05):
    """현재가와 매수구간 비교 → ('now'|'wait'|'watch', 진입가)"""
    if cur <= zone_hi:
        return "now", cur
    if (cur - zone_hi) / cur <= max_gap:
        return "wait", zone_hi
    return "watch", zone_hi


def detect_pullback(ctx: dict, rs7: Optional[float]) -> Optional[dict]:
    d = ctx["d"]
    c, h, l, v = d["c"], d["h"], d["l"], d["v"]
    n = len(c)
    if n < MIN_BARS:
        return None
    cur = ctx["cur"]
    e20, e60 = ema(c, 20), ema(c, 60)
    a = atr(h, l, c)
    if a is None or np.isnan(e20[-1]) or np.isnan(e60[-1]) or np.isnan(e60[-8]):
        return None
    if not (e20[-1] > e60[-1] and e60[-1] > e60[-8] and c[-1] > e60[-1]):
        return None                                   # 상승 추세가 아니면 눌림목이 아님
    if c[-1] / c[-2] - 1 < -0.08:
        return None                                   # 전일 투매봉 — 하루 지켜본다
    hh = float(np.max(h[-12:]))
    depth = (hh - cur) / hh * 100
    if not (4 <= depth <= 18):
        return None
    r_d = rsi(c)
    if r_d is None or not (33 <= r_d <= 62):
        return None
    if cur >= e20[-1] * 0.985:
        sup, sname = float(e20[-1]), "일봉 20일선"
    elif cur >= e60[-1] * 0.985:
        sup, sname = float(e60[-1]), "일봉 60일선"
    else:
        return None

    zone_lo, zone_hi = sup - 0.25 * a, sup + 0.35 * a
    state, entry = _state_for_zone(cur, zone_lo, zone_hi)
    stab = h1_stabilized(ctx.get("h1"))
    note = ""
    if state == "now" and stab is False:
        state, note = "watch", "1시간봉 반등(양봉) 마감 확인 후 진입"

    stop_c1 = zone_lo - 0.6 * a
    piv = [i for i in pivot_lows(l) if i >= n - 25 and l[i] < entry * 0.985]
    stop_raw = stop_c1
    if piv:
        c2 = float(l[piv[-1]]) - 0.3 * a
        if (entry - c2) / entry * 100 <= MAX_STOP_PCT * 0.9:
            stop_raw = min(stop_c1, c2)
    plan = make_plan(entry, stop_raw, state if state != "watch" else "wait", zone_lo, zone_hi)
    if not plan:
        return None
    if hh < plan["t1"]:                                # 직전 고점 아래에 1차 목표가 있어야 여유가 있다
        return None

    dry = float(np.mean(v[-3:])) < float(np.mean(v[-13:-3])) * 0.9
    score = 40 + 8 + _rs_pts(rs7) + _liq_pts(ctx["value24"]) + (6 if state == "now" else 0) + (6 if dry else 0)
    score += 4 if 40 <= r_d <= 55 else 0
    reasons = [
        "상승 추세 유지 (20일선 > 60일선, 60일선 우상향)",
        f"최근 고점 대비 -{depth:.1f}% 눌림, {sname} 지지 구간",
        f"일봉 RSI {r_d:.0f} — 과열 해소",
    ]
    if dry:
        reasons.append("눌리는 동안 거래량 감소 — 매도 압력 약화")
    risks = []
    if rs7 is not None and rs7 < 0:
        risks.append(f"최근 7일 BTC보다 약함 ({rs7:+.1f}%p)")
    if stab is None:
        risks.append("1시간봉 반등 확인 데이터 없음")
    trigger = f"{rp(zone_hi):,}원 이하로 내려오면 매수 구간" if state == "watch" and not note else note
    return {"setup": "pullback", "state": state, "plan": plan, "score": int(score), "reasons": reasons, "risks": risks,
            "trigger_text": trigger, "trigger_price": rp(zone_hi),
            "metrics": {"rsi_d": round(r_d, 1), "atr_pct": round(a / cur * 100, 2), "depth_pct": round(depth, 1)}}


def detect_breakout_retest(ctx: dict, rs7: Optional[float]) -> Optional[dict]:
    d = ctx["d"]
    c, h, l, v = d["c"], d["h"], d["l"], d["v"]
    n = len(c)
    if n < MIN_BARS:
        return None
    cur = ctx["cur"]
    a = atr(h, l, c)
    e60 = ema(c, 60)
    if a is None or np.isnan(e60[-1]) or np.isnan(e60[-6]) or e60[-1] < e60[-6] * 0.99:
        return None
    found = None
    for k in range(5, 0, -1):                          # 최근 5일 안의 가장 이른 돌파일
        idx = n - k
        prior_high = float(np.max(h[idx - 20:idx]))
        vol_avg = float(np.mean(v[idx - 20:idx]))
        if vol_avg > 0 and c[idx] > prior_high * 1.005 and v[idx] >= 1.5 * vol_avg:
            found = (idx, prior_high, float(v[idx] / vol_avg))
            break
    if not found:
        return None
    idx, level, vol_ratio = found
    if float(np.min(c[idx:])) < level * 0.985:         # 돌파 후 종가가 선 아래로 밀리면 실패한 돌파
        return None
    if cur < level * 0.985:
        return None
    zone_lo, zone_hi = level - 0.2 * a, level + 0.5 * a
    ext = (cur - level) / level * 100
    state, entry = _state_for_zone(cur, zone_lo, zone_hi)
    if state != "now" and ext > 8:
        state = "watch"
    stab = h1_stabilized(ctx.get("h1"))
    note = ""
    if state == "now" and stab is False:
        state, note = "watch", "1시간봉 반등(양봉) 마감 확인 후 진입"
    plan = make_plan(entry, level - 1.0 * a, state if state != "watch" else "wait", zone_lo, zone_hi)
    if not plan:
        return None
    r_d = rsi(c)
    score = 40 + (8 if np.nanmax([e60[-1]]) < cur else 0) + _rs_pts(rs7) + _liq_pts(ctx["value24"])
    score += 6 if state == "now" else 0
    score += 8 if vol_ratio >= 2 else 4
    reasons = [
        f"{n - idx}일 전 20일 고점({rp(level):,}원)을 거래량 {vol_ratio:.1f}배로 돌파",
        "돌파 후 종가가 돌파선 위에서 유지됨 (안착)",
        f"돌파선 부근 눌림 구간 — 현재 돌파선 대비 {ext:+.1f}%",
    ]
    risks = []
    if r_d is not None and r_d > 70:
        risks.append(f"일봉 RSI {r_d:.0f} — 과열권")
        score -= 6
    if rs7 is not None and rs7 < 0:
        risks.append(f"최근 7일 BTC보다 약함 ({rs7:+.1f}%p)")
    if stab is None:
        risks.append("1시간봉 반등 확인 데이터 없음")
    trigger = note or (f"{rp(zone_hi):,}원 부근 눌림 대기 (현재 돌파선 대비 {ext:+.1f}%)" if state == "watch" else "")
    return {"setup": "breakout_retest", "state": state, "plan": plan, "score": int(score), "reasons": reasons,
            "risks": risks, "trigger_text": trigger, "trigger_price": rp(zone_hi),
            "metrics": {"rsi_d": round(r_d, 1) if r_d is not None else None, "atr_pct": round(a / cur * 100, 2),
                        "vol_ratio": round(vol_ratio, 2), "breakout_level": rp(level)}}


def detect_squeeze(ctx: dict, rs7: Optional[float]) -> Optional[dict]:
    """저항선을 여러 번 두드리며 저점↑ + 볼밴 수렴. 돌파 전이라 '감시'로만 낸다."""
    d = ctx["d"]
    h, l, c, v = d["h"], d["l"], d["c"], d["v"]
    n = len(c)
    if n < 140:
        return None
    cur = ctx["cur"]
    widths = []
    for i in range(n - 120, n + 1):
        w = c[i - 20:i]
        m = float(np.mean(w))
        widths.append(4 * float(np.std(w, ddof=1)) / m * 100 if m > 0 else 0.0)
    w_now, w_10 = widths[-1], widths[-11]
    pct = round(sum(1 for x in widths if x < w_now) / len(widths) * 100, 1)
    if pct > 30 or w_now >= w_10:
        return None
    R = float(np.max(h[-60:-3]))
    near = [i for i in range(n - 60, n) if h[i] >= R * 0.97]
    touches, last = 0, -99
    for i in near:
        if i - last >= 3:
            touches += 1
        last = i
    if touches < 2 or (n - 1 - near[-1]) > 30:
        return None
    if float(np.min(l[-10:])) <= float(np.min(l[-30:-10])):
        return None
    dist = (R - cur) / R * 100
    if dist < -8 or dist > 12:
        return None
    if cur > R:
        status = "장중 돌파 시도 — 일봉 종가 확인"
    elif dist <= 3:
        status = "돌파 임박"
    else:
        status = "수렴 중"
    score = 30 + (15 if pct <= 10 else 8 if pct <= 20 else 4) + (10 if touches >= 3 else 5) + _rs_pts(rs7) // 2
    score += 10 if dist <= 3 else 4
    return {"setup": "squeeze", "state": "watch", "plan": None, "score": int(score),
            "reasons": [f"저항선 {rp(R):,}원을 {touches}번 두드림, 저점은 높아지는 중",
                        f"볼린저 밴드폭이 최근 120일 중 하위 {pct:.0f}% — 에너지 응축"],
            "risks": [], "trigger_price": rp(R),
            "trigger_text": f"일봉 종가가 {rp(R):,}원 위 + 거래량 1.5배↑이면 돌파 확정 → 이후 눌림 구간에서 매수 추천이 열립니다 ({status})",
            "metrics": {"band_pctile": pct, "touches": touches, "dist_pct": round(dist, 2)}}


def analyze_for_watchlist(ctx: dict, rs7: Optional[float]) -> dict:
    """내 관심종목 현황 한 줄 (매수 신호 유무와 무관하게)."""
    d = ctx["d"]
    c, h, l = d["c"], d["h"], d["l"]
    cur = ctx["cur"]
    row = {"ticker": ctx["ticker"], "price": cur, "chg24": ctx["chg24"], "trend": "데이터 부족", "note": ""}
    if len(c) < 65:
        return row
    e20, e60 = float(ema(c, 20)[-1]), float(ema(c, 60)[-1])
    r = rsi(c)
    if e20 > e60 and cur > e60:
        trend = "상승"
    elif cur < e60 and e20 < e60:
        trend = "하락"
    else:
        trend = "횡보"
    below = [x for x in (e20, e60) if x < cur]
    support = max(below) if below else None
    hi20 = float(np.max(h[-20:]))
    row.update({"trend": trend, "rsi_d": round(r, 1) if r is not None else None, "ema20": rp(e20), "ema60": rp(e60),
                "support": rp(support) if support else None, "resistance": rp(hi20) if hi20 > cur else None,
                "rs7": round(rs7, 1) if rs7 is not None else None})
    if trend == "하락":
        row["note"] = "60일선 아래 하락 추세 — 신규 매수 보류"
    elif support and (cur - support) / cur < 0.03:
        row["note"] = f"지지선({rp(support):,}원) 근접"
    elif r is not None and r > 70:
        row["note"] = "RSI 과열 — 추격 금지"
    return row


# ==========================================================
# 7. AI 악재 검증 (가격은 만들지 않는다)
# ==========================================================
def ai_veto(picks: list) -> tuple:
    """반환: (ticker→{verdict,note,positive}, 상태 'ok'|'unavailable')"""
    if not GEMINI_API_KEY or not picks:
        return {}, "unavailable"
    today = datetime.now(KST).strftime("%Y년 %m월 %d일")
    lines = "\n".join(f"- {p['ticker']} ({SETUP_LABEL.get(p['setup'], p['setup'])})" for p in picks)
    prompt = f"""오늘({today}) 기준, 아래 코인을 지금 매수해도 되는지 '악재 여부만' 구글 검색으로 확인해라.
확인 항목: ①거래소 상장폐지·투자유의·거래정지 ②5일 안의 대규모 토큰 언락 ③해킹·익스플로잇·팀 이탈 ④SEC 등 규제·소송의 새 진행 ⑤급락을 설명하는 구체적 사건
최근 7일 이내 뉴스만 사용해라. 학습 기억 속 오래된 이슈(이미 종결된 소송 등)를 현재 진행 중인 것처럼 쓰지 마라.
검색으로 확인되지 않으면 verdict는 "ok", note는 빈 문자열로 둬라. 가격·목표가·전망은 쓰지 마라.
호재는 근거가 확인될 때만 positive에 한 줄로 써라(없으면 null).
verdict: "block"(지금 매수 비권장: 상폐·해킹·대규모 언락 임박 등) / "caution"(주의 필요) / "ok"
순수 JSON만 반환:
{{"results": {{"티커": {{"verdict": "ok", "note": "", "positive": null}}}}}}
대상:
{lines}"""
    payload = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
               "generationConfig": {"temperature": 0.1}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=120)
            if r.status_code == 200:
                parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                text = " ".join(p["text"] for p in parts if "text" in p).strip()
                cleaned = re.sub(r"```json|```", "", text).strip()
                a, b = cleaned.find("{"), cleaned.rfind("}")
                if a != -1 and b != -1:
                    res = json.loads(cleaned[a:b + 1]).get("results", {})
                    out = {}
                    for tk, v in res.items():
                        if not isinstance(v, dict):
                            continue
                        verdict = v.get("verdict") if v.get("verdict") in ("ok", "caution", "block") else "ok"
                        out[str(tk).upper()] = {"verdict": verdict, "note": str(v.get("note") or "").strip(),
                                                "positive": (str(v["positive"]).strip() if v.get("positive") else None)}
                    return out, "ok"
            else:
                log(f"  ⚠️ Gemini HTTP {r.status_code}")
                time.sleep(30 if r.status_code in (429, 503) else 5)
        except Exception as e:
            log(f"  ⚠️ Gemini 오류({attempt + 1}/3): {str(e)[:100]}")
            time.sleep(5)
    return {}, "unavailable"


AI_CACHE_HOURS = float(ENV("AI_CACHE_HOURS", "24"))   # 같은 종목은 이 시간 동안 AI 재검증 안 함
AI_STICKY_HOURS = float(ENV("AI_STICKY_HOURS", "72"))  # 악재·주의 경고는 이 시간 동안 유지


def ai_veto_cached(cand: list, store, at_ms: int) -> tuple:
    """종목별 AI 판정을 AI_CACHE_HOURS 동안 재사용. 새로 확인할 종목이 있을 때만 Gemini 1회 호출.
    반환: (ticker→판정, 상태, 이번 호출 수, 오늘 누적 호출 수)"""
    cache = store.read("ai_cache.json", {})
    if not isinstance(cache, dict):
        cache = {}
    fresh = {tk: v for tk, v in cache.items() if not v.get("stale") and at_ms - v.get("checked_ts", 0) < AI_CACHE_HOURS * HOUR_MS}
    need = [p for p in cand if p["ticker"] not in fresh]
    calls, state = 0, "ok"
    if need:
        got, state = ai_veto(need)
        calls = 1 if GEMINI_API_KEY else 0
        if state == "ok":
            for p in need:
                v = got.get(p["ticker"], {"verdict": "ok", "note": "", "positive": None})
                v = {k: x for k, x in v.items() if k != "stale"}
                v.update(checked_ts=at_ms, checked_at=iso_kst(at_ms))
                # AI 판정은 검색할 때마다 흔들릴 수 있다 → 72시간 안에 나온 더 심각한 경고는 유지
                prev = cache.get(p["ticker"])
                sev = {"ok": 0, "caution": 1, "block": 2}
                if prev and at_ms - prev.get("warn_ts", prev.get("checked_ts", 0)) < AI_STICKY_HOURS * HOUR_MS \
                        and sev.get(prev.get("verdict"), 0) > sev.get(v["verdict"], 0):
                    v.update(verdict=prev["verdict"], note=prev.get("note", ""),
                             warn_ts=prev.get("warn_ts", prev.get("checked_ts")), warn_at=prev.get("warn_at", prev.get("checked_at")),
                             recheck="최근 재검색에서는 악재가 확인되지 않았지만 이전 경고를 유지합니다")
                elif v["verdict"] != "ok":
                    v.update(warn_ts=at_ms, warn_at=iso_kst(at_ms))
                fresh[p["ticker"]] = v
    # 만료됐어도 72시간 안의 경고는 다음 재검증 때 쓰도록 보관
    for tk, v in cache.items():
        if tk not in fresh and v.get("verdict") != "ok" and at_ms - v.get("warn_ts", v.get("checked_ts", 0)) < AI_STICKY_HOURS * HOUR_MS:
            fresh[tk] = {**v, "stale": True}
    out = {tk: v for tk, v in fresh.items() if not v.get("stale") and any(p["ticker"] == tk for p in cand)}
    if state != "ok" and any(p["ticker"] not in out for p in cand):
        state = "partial" if out else "unavailable"
    store.write("ai_cache.json", fresh, "AI 검증 캐시")
    usage = store.read("ai_usage.json", {})
    if not isinstance(usage, dict):
        usage = {}
    day = datetime.fromtimestamp(at_ms / 1000, KST).strftime("%Y-%m-%d")
    usage[day] = usage.get(day, 0) + calls
    usage = dict(sorted(usage.items())[-60:])
    store.write("ai_usage.json", usage, "AI 호출 기록")
    return out, state, calls, usage[day]


# ==========================================================
# 8. 장부(ledger): 고정된 계획을 캔들로 채점
# ==========================================================
def new_entry(p: dict, at_ms: int, gate: str) -> dict:
    now = p["state"] == "now"
    return {
        "id": f"{p['ticker']}-{p['setup']}-{datetime.fromtimestamp(at_ms / 1000, KST).strftime('%Y%m%d%H%M')}",
        "issued_ts": at_ms, "issued_at": iso_kst(at_ms), "ticker": p["ticker"], "setup": p["setup"],
        "state": p["state"], "ref_price": p["price"], "entry": p["entry"], "zone_lo": p["zone_lo"], "zone_hi": p["zone_hi"],
        "stop": p["stop"], "t1": p["t1"], "t2": p["t2"], "gate": gate, "score": p["score"],
        "valid_until_ts": at_ms + (0 if now else WAIT_VALID_HOURS * HOUR_MS),
        "cursor_ts": (at_ms // HOUR_MS + 1) * HOUR_MS,
        "status": "open" if now else "pending",
        "fill_ts": at_ms if now else None, "fill_price": p["price"] if now else None,
        "t1_hit": False, "t1_ts": None, "exit_ts": None, "exit_price": None, "exit_reason": None,
        "r_gross": None, "r_net": None, "mfe_r": 0.0, "mae_r": 0.0, "last_price": p["price"],
    }


def _close(e: dict, ts: float, price: float, reason: str, events: list):
    e["status"] = "closed"
    e["exit_ts"], e["exit_price"], e["exit_reason"] = int(ts), price, reason
    risk = e["entry"] - e["stop"]
    fill = e["fill_price"]
    if risk > 0 and fill:
        if e["t1_hit"]:
            gross = T1_SELL_FRACTION * (e["t1"] - fill) / risk + (1 - T1_SELL_FRACTION) * (price - fill) / risk
        else:
            gross = (price - fill) / risk
        cost = 2 * FEE_PCT_PER_SIDE / 100 * fill / risk
        e["r_gross"], e["r_net"] = round(gross, 3), round(gross - cost, 3)
    why = {"stop": "손절", "be_stop": "본전 청산", "t2": "2차 목표 도달", "time": "5일 타임스톱"}.get(reason, reason)
    events.append(f"{label(e['ticker'])} {SETUP_LABEL.get(e['setup'], e['setup'])} — {why} (순수익 {e['r_net']:+.2f}R)")


def replay_entry(e: dict, cd: dict, at_ms: float, bar_ms: int = HOUR_MS, max_hold_ms: int = MAX_HOLD_HOURS * HOUR_MS) -> list:
    """완성된 캔들을 순서대로 적용. 같은 캔들에서 손절·목표가 모두 가능하면 손절 우선. 이벤트 문자열 목록 반환."""
    events = []
    if e["status"] not in ("pending", "open"):
        return events
    for i in range(len(cd["t"])):
        t = float(cd["t"][i])
        if t < e["cursor_ts"]:
            continue
        if t + bar_ms > at_ms:
            break
        o, h, l, c = (float(cd[k][i]) for k in ("o", "h", "l", "c"))
        e["cursor_ts"] = int(t + bar_ms)
        e["last_price"] = c
        fill_candle = False

        if e["status"] == "pending":
            if t >= e["valid_until_ts"]:
                e["status"], e["exit_reason"], e["exit_ts"] = "expired", "unfilled", int(t)
                events.append(f"{label(e['ticker'])} {SETUP_LABEL.get(e['setup'], e['setup'])} — 유효시간 내 미체결(만료)")
                return events
            if l <= e["entry"]:
                if o <= e["stop"]:                      # 손절선 아래로 갭 — 진입 무효
                    e["status"], e["exit_reason"], e["exit_ts"] = "expired", "invalidated", int(t)
                    events.append(f"{label(e['ticker'])} {SETUP_LABEL.get(e['setup'], e['setup'])} — 체결 전 손절선 이탈(무효)")
                    return events
                e["status"], e["fill_ts"], e["fill_price"] = "open", int(t), min(e["entry"], o)
                fill_candle = True
                events.append(f"{label(e['ticker'])} {SETUP_LABEL.get(e['setup'], e['setup'])} — 지정가 체결 {fmt_krw(e['fill_price'])}")
            else:
                continue

        # 여기부터 status == open
        risk = e["entry"] - e["stop"]
        fill = e["fill_price"]
        if risk > 0:
            e["mfe_r"] = round(max(e["mfe_r"], (h - fill) / risk), 3)
            e["mae_r"] = round(min(e["mae_r"], (l - fill) / risk), 3)
        if not e["t1_hit"]:
            if l <= e["stop"]:
                _close(e, t + bar_ms, min(e["stop"], o), "stop", events)
                return events
            if (not fill_candle) and h >= e["t1"]:
                e["t1_hit"], e["t1_ts"] = True, int(t)
                events.append(f"{label(e['ticker'])} {SETUP_LABEL.get(e['setup'], e['setup'])} — 1차 목표 도달 (절반 매도, 손절 본전으로)")
        else:
            if t > e["t1_ts"]:
                if l <= fill:
                    _close(e, t + bar_ms, min(fill, o), "be_stop", events)
                    return events
                if h >= e["t2"]:
                    _close(e, t + bar_ms, max(e["t2"], o), "t2", events)
                    return events
        if e["status"] == "open" and e["fill_ts"] is not None and (t + bar_ms - e["fill_ts"]) >= max_hold_ms:
            _close(e, t + bar_ms, c, "time", events)
            return events
    return events


def _agg(rows: list) -> dict:
    closed = [e for e in rows if e["status"] == "closed" and e.get("r_net") is not None]
    filled = [e for e in rows if e.get("fill_ts")]
    expired = [e for e in rows if e["status"] == "expired"]
    rs = [e["r_net"] for e in closed]
    pos, neg = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
    out = {
        "n_signals": len(rows), "n_filled": len(filled), "n_unfilled": len(expired),
        "n_open": sum(1 for e in rows if e["status"] == "open"), "n_pending": sum(1 for e in rows if e["status"] == "pending"),
        "fill_rate": round(len(filled) / (len(filled) + len(expired)) * 100, 1) if (filled or expired) else None,
        "n_closed": len(closed),
        "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1) if rs else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "median_r": round(statistics.median(rs), 3) if rs else None,
        "profit_factor": round(pos / neg, 2) if neg > 0 else None,
        "avg_mfe_r": round(sum(e["mfe_r"] for e in closed) / len(closed), 2) if closed else None,
        "avg_mae_r": round(sum(e["mae_r"] for e in closed) / len(closed), 2) if closed else None,
        "reliable": len(closed) >= 20,
    }
    return out


def compute_stats(ledger: list) -> dict:
    by_setup, by_gate = {}, {}
    for key, target in (("setup", by_setup), ("gate", by_gate)):
        for val in sorted({e.get(key) for e in ledger if e.get(key)}):
            target[val] = _agg([e for e in ledger if e.get(key) == val])
    closed = sorted([e for e in ledger if e["status"] == "closed" and e.get("r_net") is not None], key=lambda e: e["exit_ts"])
    cum, curve = 0.0, []
    for e in closed:
        cum += e["r_net"]
        curve.append({"ts": e["exit_ts"], "r": round(cum, 3), "ticker": e["ticker"]})
    return {"generated_at": iso_kst(now_ms()), "fee_pct_per_side": FEE_PCT_PER_SIDE, "overall": _agg(ledger),
            "by_setup": by_setup, "by_gate": by_gate, "curve": curve,
            "exit_reasons": {r: sum(1 for e in closed if e["exit_reason"] == r) for r in sorted({e["exit_reason"] for e in closed})}}


def track_ledger(ledger: list, at_ms: int) -> list:
    """활성 항목의 1시간봉을 받아 재생. 이벤트 목록 반환."""
    events = []
    active = [e for e in ledger if e["status"] in ("pending", "open")]
    if not active:
        return events
    cache = {}
    tickers = sorted({e["ticker"] for e in active})
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_candles, tk, "1h", 400): tk for tk in tickers}
        for f in as_completed(futs):
            try:
                cache[futs[f]] = f.result()
            except Exception:
                cache[futs[f]] = None
    for e in active:
        cd = cache.get(e["ticker"])
        if not cd:
            log(f"  ⚠️ {e['ticker']} 1시간봉 수집 실패 — 이번 회차 채점 건너뜀")
            continue
        events += replay_entry(e, cd, at_ms)
    return events


def has_recent_stop(ledger: list, ticker: str, at_ms: int) -> bool:
    for e in ledger:
        if e["ticker"] == ticker and e.get("exit_reason") == "stop" and e.get("exit_ts") and at_ms - e["exit_ts"] < COOLDOWN_HOURS * HOUR_MS:
            return True
    return False


# ==========================================================
# 9. 스캔 파이프라인
# ==========================================================
def build_universe(tickers: dict) -> list:
    rows = [(tk, v) for tk, v in tickers.items() if tk not in STABLES and v["value"] >= MIN_VALUE_KRW]
    rows.sort(key=lambda x: x[1]["value"], reverse=True)
    uni = [tk for tk, _ in (rows[:UNIVERSE_N] if UNIVERSE_N > 0 else rows)]
    for tk in ["BTC"] + WATCHLIST:                      # BTC(시장판단)와 관심종목은 항상 포함
        if tk in tickers and tk not in uni:
            uni.append(tk)
    return uni


def gather(tickers: dict, universe: list, at_ms: int) -> tuple:
    ctxs, failed = {}, []

    def one(tk):
        dcd = fetch_candles(tk, "24h", 300)
        return tk, ((dcd, None) if dcd else None)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(one, tk) for tk in universe]):
            try:
                tk, res = f.result()
            except Exception:
                continue
            if not res:
                failed.append(tk)
                continue
            dcd, hcd = res
            t = tickers[tk]
            ctxs[tk] = {"ticker": tk, "cur": t["price"], "chg24": t["chg"], "value24": t["value"],
                        "d": slice_completed(dcd, DAY_MS, at_ms),
                        "h1": slice_completed(hcd, HOUR_MS, at_ms) if hcd else None}
    return ctxs, failed


def attach_h1(ctxs: dict, tickers: set, at_ms: int):
    """1시간봉은 매수 후보에만 받는다 (전 종목에 받으면 요청이 두 배)."""
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_candles, tk, "1h", 120): tk for tk in tickers if tk in ctxs}
        for f in as_completed(futs):
            try:
                h = f.result()
            except Exception:
                h = None
            if h:
                ctxs[futs[f]]["h1"] = slice_completed(h, HOUR_MS, at_ms)


def funnel_stats(ctxs: dict, picks: list, watch: list) -> dict:
    """추천이 왜 이만큼인지: 단계별로 몇 종목이 남는지."""
    n_all = n_hist = n_up = n_down = n_side = n_depth = n_rsi = 0
    for tk, cx in ctxs.items():
        if tk == "BTC":
            continue
        n_all += 1
        c = cx["d"]["c"]
        if len(c) < MIN_BARS:
            continue
        n_hist += 1
        e20, e60 = ema(c, 20), ema(c, 60)
        if np.isnan(e60[-8]):
            continue
        up = e20[-1] > e60[-1] and e60[-1] > e60[-8] and c[-1] > e60[-1]
        down = cx["cur"] < e60[-1] and e20[-1] < e60[-1]
        up, down = bool(up), bool(down)
        n_up += up
        n_down += down
        n_side += (not up and not down)
        if not up:
            continue
        hh = float(np.max(cx["d"]["h"][-12:]))
        depth = (hh - cx["cur"]) / hh * 100
        if 4 <= depth <= 18:
            n_depth += 1
            r = rsi(c)
            if r is not None and 33 <= r <= 62:
                n_rsi += 1
    return {"scanned": n_all, "enough_history": n_hist, "uptrend": n_up, "downtrend": n_down, "sideways": n_side,
            "uptrend_pulled_back": n_depth, "pullback_rsi_ok": n_rsi,
            "setups": len(picks) + len([w for w in watch if w.get("setup") != "squeeze"]),
            "squeeze": len([w for w in watch if w.get("setup") == "squeeze"]), "picks": len(picks),
            "min_history_days": MIN_BARS, "min_value_krw": MIN_VALUE_KRW}


# ── 4시간 RSI 상승 다이버전스 (2년 백테스트로 고른 규칙) ──────────────────
#  일봉 상승추세 + 4시간봉 가격 저점↓·RSI 저점↑(두 번째 저점 RSI<40) + 반등 거래량 붙음
#  거래량 붙음 = 저점 이후 4시간봉 3개 중 하나라도 직전 5일(30봉) 평균 거래대금의 1.0배 이상
#  (거래량 안 붙은 다이버전스는 백테스트에서 손실: PF 0.80 / 붙은 경우 PF 4.8, 30건)
DIV_K = 3                  # 저점 판정: 좌우 3봉보다 낮음
DIV_RSI_MAX = 40
DIV_VOL_MIN, DIV_VOL_HOT = 1.0, 3.0
H4_MS = 4 * HOUR_MS


def fetch_h4(ticker: str, count: int = 200) -> Optional[dict]:
    """빗썸 v1 240분봉 → dict(t,o,h,l,c,v). v는 거래대금(원)."""
    j = http_json("https://api.bithumb.com/v1/candles/minutes/240", params={"market": f"KRW-{ticker}", "count": count}, retries=2)
    if not isinstance(j, list) or len(j) < 60:
        return None
    rows = []
    for r in reversed(j):
        try:
            t = datetime.fromisoformat(r["candle_date_time_kst"]).replace(tzinfo=KST).timestamp() * 1000
            o, h, l, c = (float(r[k]) for k in ("opening_price", "high_price", "low_price", "trade_price"))
            v = float(r.get("candle_acc_trade_price") or 0)
        except Exception:
            continue
        lo_, hi_ = min(o, c), max(o, c)
        l = lo_ if l < lo_ * 0.7 else l           # 오류 체결 꼬리 보정
        h = hi_ if h > hi_ * 1.5 else h
        rows.append((t, o, h, l, c, v))
    a = np.array(rows, float)
    return {"t": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def rsi_series(c, n: int = 14) -> np.ndarray:
    out = np.full(len(c), np.nan)
    d = np.diff(c)
    if len(d) < n:
        return out
    g, ls = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = g[:n].mean(), ls[:n].mean()
    out[n] = 100 - 100 / (1 + ag / al) if al > 0 else 100.0
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + ls[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al > 0 else 100.0
    return out


def detect_div4h(ticker: str, h4: dict, at_ms: float, max_age_bars: int = 1) -> Optional[dict]:
    """완성된 4시간봉 기준. 두 번째 저점이 확정된 봉(저점+3봉)이 최근 max_age_bars 안이면 신호."""
    h4 = slice_completed(h4, H4_MS, at_ms)
    o, h, l, c, v, t = h4["o"], h4["h"], h4["l"], h4["c"], h4["v"], h4["t"]
    n = len(c)
    if n < 60:
        return None
    R = rsi_series(c)
    piv = [i for i in range(DIV_K, n - DIV_K) if l[i] == l[i - DIV_K:i + DIV_K + 1].min()]
    if len(piv) < 2:
        return None
    x, y = piv[-2], piv[-1]
    sig_i = y + DIV_K
    if sig_i > n - 1 or sig_i < n - max_age_bars:
        return None
    if not (y - x <= 30 and l[y] < l[x] and R[y] > R[x] + 2 and R[y] < DIV_RSI_MAX):
        return None
    base = v[max(0, y - 30):y].mean() if y >= 10 else 0
    if base <= 0:
        return None
    vol_x = float(v[y + 1:sig_i + 1].max() / base)
    low_smaller = bool(v[y - 1:y + 2].sum() < v[x - 1:x + 2].sum())
    a = atr(h, l, c) or 0
    return {"pivot_ts": int(t[y]), "signal_ts": int(t[sig_i]), "low1": float(l[x]), "low2": float(l[y]),
            "rsi1": round(float(R[x]), 1), "rsi2": round(float(R[y]), 1), "vol_x": round(vol_x, 2),
            "vol_ok": DIV_VOL_MIN <= vol_x, "vol_hot": vol_x >= DIV_VOL_HOT, "low_vol_smaller": low_smaller,
            "swing_low": float(l[max(0, sig_i - 5):sig_i + 1].min()), "atr4h": float(a), "close": float(c[sig_i])}


def daily_uptrend(d: dict) -> bool:
    c = d["c"]
    if len(c) < 70:
        return False
    e20, e60 = ema(c, 20), ema(c, 60)
    return bool(c[-1] > e60[-1] and e20[-1] > e60[-1] and e60[-1] > e60[-6])


def div4h_pick(ticker: str, sig: dict, price: float, chg24: float = 0.0, value24: float = 0.0) -> Optional[dict]:
    entry = price
    stop = sig["swing_low"] - 0.2 * sig["atr4h"]
    plan = make_plan(entry, stop, "now", min(entry, sig["close"]) * 0.995, max(entry, sig["close"]) * 1.005, max_stop=10.0)
    if not plan:
        return None
    nm_t = datetime.fromtimestamp(sig["pivot_ts"] / 1000, KST).strftime("%m/%d %H시")
    reasons = ["일봉 상승추세 (20일선 > 60일선, 60일선 우상향)",
               f"4시간봉 저점 {fmt_krw(sig['low1'])} → {fmt_krw(sig['low2'])}(낮아짐), RSI {sig['rsi1']:.0f} → {sig['rsi2']:.0f}(높아짐)",
               f"반등 거래량 평소의 {sig['vol_x']:.1f}배"]
    risks = []
    if sig["low_vol_smaller"]:
        reasons.append("두 번째 저점 거래량이 첫 번째보다 적음 — 매도세 약화")
    else:
        risks.append("두 번째 저점 거래량이 첫 번째보다 많음 — 신호 약함")
    if sig["vol_hot"]:
        risks.append(f"반등 거래량 {sig['vol_x']:.1f}배 — 과열(3배↑) 구간은 백테스트에서 성과가 나빴음")
    score = 70 + (8 if sig["low_vol_smaller"] else 0) - (15 if sig["vol_hot"] else 0)
    p = {"ticker": ticker, "setup": "div4h", "setup_label": SETUP_LABEL["div4h"], "state": "now", "price": price, "chg24": chg24,
         "entry": plan["entry"], "zone_lo": plan["zone_lo"], "zone_hi": plan["zone_hi"], "stop": plan["stop"], "t1": plan["t1"], "t2": plan["t2"],
         "stop_pct": plan["stop_pct"], "t1_pct": plan["t1_pct"], "t2_pct": plan["t2_pct"], "rr1": plan["rr1"], "rr2": plan["rr2"],
         "score": score, "reasons": reasons, "risks": risks, "metrics": {k: sig[k] for k in ("rsi1", "rsi2", "vol_x", "low_vol_smaller")},
         "rs7": None, "value24": value24, "ai": {"status": "unverified", "note": "", "positive": None},
         "valid_hours": 0, "signal_at": iso_kst(sig["signal_ts"] + H4_MS), "pivot_at": nm_t,
         "sig_key": f"{ticker}-{sig['pivot_ts']}", "vol_ok": sig["vol_ok"]}
    return p


def scan_div4h(tickers: list, at_ms: int, price_map: dict, max_age_bars: int = 1) -> tuple:
    """일봉 상승추세 종목만 4시간봉 확인. 반환: (거래량 붙은 신호, 거래량 안 붙은 참고 신호)"""
    good, weak = [], []

    def one(tk):
        h = fetch_h4(tk)
        return tk, (detect_div4h(tk, h, at_ms, max_age_bars) if h else None)

    with ThreadPoolExecutor(max_workers=5) as ex:
        for f in as_completed([ex.submit(one, tk) for tk in tickers]):
            try:
                tk, sig = f.result()
            except Exception:
                continue
            if not sig:
                continue
            info = price_map.get(tk, {})
            p = div4h_pick(tk, sig, info.get("price") or sig["close"], info.get("chg", 0.0), info.get("value", 0.0))
            if not p:
                continue
            (good if sig["vol_ok"] else weak).append(p)
    return good, weak


def record_div4h(store, good: list, weak: list, ledger: list, at_ms: int, gate: str) -> list:
    """새 신호만 장부에 기록하고 data/div4h.json(최근 7일 신호 목록)을 갱신. 새로 나온 '거래량 붙은' 신호를 반환."""
    seen = {e.get("sig_key") for e in ledger if e.get("sig_key")}
    hist = store.read("div4h.json", {"signals": []})
    if not isinstance(hist, dict):
        hist = {"signals": []}
    known = {x.get("sig_key") for x in hist["signals"]}
    fresh = []
    for p in good:
        if p["sig_key"] in seen:
            continue
        e = new_entry(p, at_ms, gate)
        e["sig_key"] = p["sig_key"]
        ledger.append(e)
        p["id"], p["issued_at"] = e["id"], e["issued_at"]
        fresh.append(p)
    for p in good + weak:
        if p["sig_key"] not in known:
            hist["signals"].append({**p, "found_at": iso_kst(at_ms), "found_ts": at_ms, "name": kname(p["ticker"])})
    hist["signals"] = [x for x in hist["signals"] if at_ms - x.get("found_ts", at_ms) < 7 * DAY_MS][-60:]
    hist["checked_at"] = iso_kst(at_ms)
    store.write("div4h.json", hist, "4시간 다이버전스")
    return fresh


def slack_div4h(fresh: list):
    if not fresh:
        return
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"⚡ 4시간 다이버전스 신호 {len(fresh)}개", "emoji": True}},
              _ctx("일봉 상승추세 + 4시간 RSI 상승 다이버전스 + 반등 거래량 · 2년 백테스트 승률 77% (30건, 표본 적음) · AI 악재 확인 없음")]
    for p in fresh:
        blocks.append({"type": "divider"})
        blocks += _pick_blocks(p)
        if p["risks"]:
            blocks.append(_ctx("⚠️ " + " · ".join(p["risks"])))
    blocks.append(_ctx(f"<{DASHBOARD_URL}|대시보드 열기> · 투자 판단과 책임은 본인에게 있습니다"))
    slack_post(f"⚡ 4시간 다이버전스 {', '.join(label(p['ticker']) for p in fresh)}", blocks)


# ── 섹터 온도 ─────────────────────────────────────────────────────────
#  data/sector_map.json (CoinGecko 카테고리 + 과거 데이터로 뽑은 섹터별 대장)을 읽어
#  빗썸 24시간 변동률로 섹터별 온도를 계산. 매수 신호가 아니라 '돈이 어디로 몰리는지' 정보.
SECTOR_HOT_N, SECTOR_HOT_PCT, SECTOR_MED_HOT = 2, 10.0, 5.0
SECTOR_ALERT_COOL_H = 12


def sector_heat(tickers: dict, smap: dict) -> list:
    rows = []
    for g, info in (smap.get("groups") or {}).items():
        mem = [t for t in info.get("members", []) if t in tickers]
        if len(mem) < 3:
            continue
        ch = {t: float(tickers[t]["chg"]) for t in mem}
        vals = sorted(ch.values())
        med = float(np.median(vals))
        n10 = sum(v >= SECTOR_HOT_PCT for v in vals)
        lead_tks = [x["tk"] for x in info.get("leaders", [])]
        movers = sorted(mem, key=lambda t: -ch[t])
        need = max(SECTOR_HOT_N, math.ceil(0.15 * len(mem)))     # 큰 섹터는 15% 이상이 움직여야 '뜨거움'
        hot = n10 >= need or med >= SECTOR_MED_HOT
        rows.append({
            "group": g, "n": len(mem), "median": round(med, 2), "n10": n10, "n5": sum(v >= 5 for v in vals),
            "hot": bool(hot), "need": need, "top": [{"tk": t, "chg": round(ch[t], 2), "name": kname(t), "leader": t in lead_tks} for t in movers[:6]],
            "leaders": [{"tk": t, "chg": round(ch[t], 2), "name": kname(t),
                         "excess": next((x["excess"] for x in info["leaders"] if x["tk"] == t), None)} for t in lead_tks if t in ch],
            "laggard_leaders": [t for t in lead_tks if t in ch and ch[t] < 3.0],
            "value": round(sum(float(tickers[t].get("value") or 0) for t in mem)),
        })
    rows.sort(key=lambda r: (-int(r["hot"]), -(r["n10"] / r["n"]), -(r["n5"] / r["n"]), -r["median"]))
    return rows


def sector_alert(store, rows: list, at_ms: int) -> list:
    """새로 뜨거워진 섹터만 (같은 섹터는 12시간에 한 번) 슬랙 알림."""
    st = store.read("sector_alerts.json", {}) or {}
    fresh = [r for r in rows if r["hot"] and at_ms - st.get(r["group"], 0) >= SECTOR_ALERT_COOL_H * HOUR_MS]
    if not fresh:
        return []
    for r in fresh:
        st[r["group"]] = at_ms
    store.write("sector_alerts.json", st, "섹터 알림 기록")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🔥 섹터 온도 상승: " + ", ".join(r["group"] for r in fresh), "emoji": True}},
              _ctx("섹터 종목의 15% 이상(최소 2개)이 24시간 +10%↑ 또는 섹터 중앙값 +5%↑ · 매수 신호 아님 — 돈이 어디로 몰리는지 정보입니다")]
    for r in fresh:
        top = " · ".join(f"{'★' if x['leader'] else ''}{x['name'] or x['tk']} {x['chg']:+.1f}%" for x in r["top"][:5])
        lag = ", ".join(kname(t) or t for t in r["laggard_leaders"])
        txt = f"*{r['group']}* — 중앙값 {r['median']:+.1f}% · +10% 넘은 종목 {r['n10']}/{r['n']}\n{top}"
        if lag:
            txt += f"\n_이 섹터에서 평소 크게 움직이던 대장 중 아직 덜 오른 종목: {lag}_"
        blocks.append(_md(txt))
    blocks.append(_ctx("★ = 과거 이 섹터가 뜰 때 평균보다 더 크게 움직였던 종목. 과거 데이터상 덜 오른 종목이 뒤따라 오른다는 보장은 없습니다."))
    slack_post("🔥 섹터 온도 상승", blocks)
    return fresh


def rel_strength(ctx: dict, btc_ret7: Optional[float]) -> Optional[float]:
    c = ctx["d"]["c"]
    if len(c) < 8 or btc_ret7 is None:
        return None
    return (ctx["cur"] / c[-7] - 1) * 100 - btc_ret7


def scan_candidates(ctxs: dict, btc_ret7: Optional[float]) -> tuple:
    picks, watch = [], []
    for tk, cx in ctxs.items():
        if tk == "BTC" or tk in STABLES:
            continue
        rs7 = rel_strength(cx, btc_ret7)
        found = [r for r in (detect_pullback(cx, rs7), detect_breakout_retest(cx, rs7)) if r]
        if found:
            best = max(found, key=lambda r: (r["state"] in ("now", "wait"), r["score"]))
            best.update({"ticker": tk, "price": cx["cur"], "chg24": cx["chg24"], "value24": cx["value24"],
                         "rs7": round(rs7, 1) if rs7 is not None else None})
            (picks if best["state"] in ("now", "wait") else watch).append(best)
            continue
        sq = detect_squeeze(cx, rs7)
        if sq:
            sq.update({"ticker": tk, "price": cx["cur"], "chg24": cx["chg24"], "value24": cx["value24"],
                       "rs7": round(rs7, 1) if rs7 is not None else None})
            watch.append(sq)
    return picks, watch


def flatten_pick(c: dict, gate: str) -> dict:
    p = c["plan"]
    return {"ticker": c["ticker"], "setup": c["setup"], "setup_label": SETUP_LABEL[c["setup"]], "state": c["state"],
            "price": c["price"], "chg24": c["chg24"], "entry": p["entry"], "zone_lo": p["zone_lo"], "zone_hi": p["zone_hi"],
            "stop": p["stop"], "t1": p["t1"], "t2": p["t2"], "stop_pct": p["stop_pct"], "t1_pct": p["t1_pct"],
            "t2_pct": p["t2_pct"], "rr1": p["rr1"], "rr2": p["rr2"], "score": c["score"], "reasons": c["reasons"],
            "risks": list(c["risks"]), "metrics": c.get("metrics", {}), "rs7": c.get("rs7"), "value24": c["value24"],
            "gate_at_issue": gate, "ai": {"status": "unverified", "note": "", "positive": None},
            "valid_hours": 0 if c["state"] == "now" else WAIT_VALID_HOURS}


def flatten_watch(c: dict, note: str = "") -> dict:
    p = c.get("plan")
    d = {"ticker": c["ticker"], "setup": c["setup"], "setup_label": SETUP_LABEL[c["setup"]], "price": c["price"],
         "chg24": c["chg24"], "trigger_price": c.get("trigger_price"), "trigger_text": c.get("trigger_text", ""),
         "dist_pct": round((c["price"] / c["trigger_price"] - 1) * 100, 2) if c.get("trigger_price") else None,
         "score": c["score"], "reasons": c["reasons"], "risks": list(c["risks"]), "note": note,
         "preview": ({k: p[k] for k in ("entry", "stop", "t1", "t2", "stop_pct", "t1_pct", "t2_pct")} if p else None)}
    return d


def apply_ledger_to_picks(picks: list, ledger: list, at_ms: int, gate: str) -> list:
    """같은 종목·셋업의 활성 추천이 있으면 그 계획(숫자)을 그대로 쓴다. 없으면 장부에 새로 기록."""
    out = []
    for p in picks:
        active = next((e for e in ledger if e["ticker"] == p["ticker"] and e["setup"] == p["setup"]
                       and e["status"] in ("pending", "open")), None)
        if active:
            for k in ("entry", "zone_lo", "zone_hi", "stop", "t1", "t2"):
                p[k] = active[k]
            risk = active["entry"] - active["stop"]
            p["stop_pct"] = round(risk / active["entry"] * 100, 2)
            p["t1_pct"] = round((active["t1"] / active["entry"] - 1) * 100, 2)
            p["t2_pct"] = round((active["t2"] / active["entry"] - 1) * 100, 2)
            p["id"], p["carried"], p["issued_at"] = active["id"], True, active["issued_at"]
            p["state"] = "now" if active["status"] == "open" and active["state"] == "now" else active["state"]
        else:
            e = new_entry(p, at_ms, gate)
            ledger.append(e)
            p["id"], p["carried"], p["issued_at"] = e["id"], False, e["issued_at"]
        out.append(p)
    return out


def run_scan() -> dict:
    t0 = time.time()
    store = Store()
    at = now_ms()
    log("🚀 코주부 시그널 엔진 v5 — 스캔 시작", iso_kst(at))

    tickers = fetch_tickers_all()
    if not tickers:
        raise SystemExit("❌ 빗썸 시세 수집 실패 — 종료")
    universe = build_universe(tickers)
    fetch_korean_names(store)
    log(f"  🔤 한글명 {len(KR_NAMES)}개")
    log(f"📡 유니버스 {len(universe)}종 (거래대금 ≥ {MIN_VALUE_KRW / 1e8:.0f}억) 캔들 수집 중...")
    ctxs, failed = gather(tickers, universe, at)
    log(f"  ✅ 수집 {len(ctxs)}종 / 실패 {len(failed)}종")

    taker = fetch_taker_ratio()
    market = compute_market(ctxs, taker)
    gate = market["gate"]
    log(f"🚦 시장: {gate} — {market['headline']}")

    btc_ret7 = None
    if "BTC" in ctxs and len(ctxs["BTC"]["d"]["c"]) >= 8:
        btc_ret7 = (ctxs["BTC"]["cur"] / ctxs["BTC"]["d"]["c"][-7] - 1) * 100

    ledger = store.read("ledger.json", [])
    if not isinstance(ledger, list):
        ledger = []
    events = track_ledger(ledger, at)          # 먼저 기존 추천 채점 (쿨다운 판단에 필요)

    raw_picks, raw_watch = scan_candidates(ctxs, btc_ret7)
    if raw_picks:                                   # 후보만 1시간봉 받아 반등 확인 후 다시 판정
        attach_h1(ctxs, {c["ticker"] for c in raw_picks}, at)
        raw_picks, raw_watch = scan_candidates(ctxs, btc_ret7)
    excluded = []
    ai_calls, ai_calls_today = 0, None

    # 쿨다운: 최근 손절 난 종목은 재추천 금지
    keep = []
    for c in raw_picks:
        if has_recent_stop(ledger, c["ticker"], at):
            excluded.append({"ticker": c["ticker"], "setup": c["setup"], "setup_label": SETUP_LABEL[c["setup"]], "kind": "쿨다운",
                             "reason": f"최근 {COOLDOWN_HOURS}시간 안에 손절된 종목", "note": "같은 종목 연속 손절을 막기 위한 규칙입니다.",
                             "price": c["price"], "plan": None, "reasons": c["reasons"]})
        else:
            keep.append(c)
    raw_picks = keep

    watch = [flatten_watch(c) for c in raw_watch]
    picks = []
    if gate in ("GO", "SELECTIVE"):
        floor = MIN_SCORE[gate]
        ranked = sorted([c for c in raw_picks if c["score"] >= floor], key=lambda c: c["score"], reverse=True)
        for c in raw_picks:
            if c not in ranked:
                watch.append(flatten_watch({**c, "state": "watch", "trigger_text": f"점수 {c['score']} < 기준 {floor} — 관찰만",
                                            "trigger_price": c["plan"]["zone_hi"]}))
        cand = [flatten_pick(c, gate) for c in ranked[:PICK_CAP[gate]]]
        verdicts, ai_state, ai_calls, ai_calls_today = ai_veto_cached(cand, store, at)
        final = []
        for p in cand:
            v = verdicts.get(p["ticker"])
            if v:
                p["ai"] = {"status": v["verdict"], "note": v["note"], "positive": v.get("positive"), "checked_at": v.get("warn_at") or v.get("checked_at"),
                           "recheck": v.get("recheck")}
                # 악재는 제외하지 않는다 — 내용을 보여주고 판단은 사용자가 한다
                if v["verdict"] == "ok":
                    p["ai"]["status"] = "ok"
            elif ai_state == "ok":
                p["ai"]["status"] = "ok"
            final.append(p)
        final.sort(key=lambda p: p["score"], reverse=True)
        picks = apply_ledger_to_picks(final[:PICK_CAP[gate]], ledger, at, gate)
    else:
        for c in raw_picks:
            watch.append(flatten_watch({**c, "state": "watch", "trigger_price": c["plan"]["zone_hi"]},
                                       note="시장 신호등이 매수 금지 — 관찰만"))
        ai_state, ai_calls, ai_calls_today = "skipped", 0, None

    watch.sort(key=lambda w: (-w["score"], abs(w["dist_pct"] or 99)))
    watch = watch[:25]

    wl_rows = []
    for tk in WATCHLIST:
        cx = ctxs.get(tk)
        if cx:
            row = analyze_for_watchlist(cx, rel_strength(cx, btc_ret7))
            row["flag"] = ("추천 중" if any(p["ticker"] == tk for p in picks)
                           else "감시 중" if any(w["ticker"] == tk for w in watch) else "")
            wl_rows.append(row)

    latest = {"version": 2, "generated_at": iso_kst(at), "generated_ts": at, "horizon": "2~5일 스윙",
              "market": market, "picks": picks, "watch": watch, "excluded": excluded, "watchlist": wl_rows,
              "pumps": live_pumps(ctxs),
              "funnel": funnel_stats(ctxs, picks, watch),
              "universe": {"count": len(universe), "scanned": len(ctxs), "failed": failed},
              "ai_status": ai_state, "ai_calls": ai_calls, "ai_calls_today": ai_calls_today, "params": {"min_stop_pct": MIN_STOP_PCT, "max_stop_pct": MAX_STOP_PCT, "t1_r": T1_R,
                                               "t2_r": T2_R, "max_hold_hours": MAX_HOLD_HOURS, "wait_valid_hours": WAIT_VALID_HOURS,
                                               "fee_pct_per_side": FEE_PCT_PER_SIDE}}
    smap = store.read("sector_map.json", {}) or {}
    latest["sectors"] = sector_heat(tickers, smap)
    up_list = sorted(tk for tk, cx in ctxs.items() if tk not in STABLES and daily_uptrend(cx["d"]))
    latest["uptrend"] = up_list
    d_good, d_weak = scan_div4h(up_list, at, tickers, max_age_bars=2)
    d_fresh = record_div4h(store, d_good, d_weak, ledger, at, gate)
    latest["div4h"] = d_good
    log(f"⚡ 4시간 다이버전스: 거래량 붙음 {len(d_good)} / 안 붙음 {len(d_weak)} (새 신호 {len(d_fresh)})")
    shown = {p["ticker"] for p in picks} | {w["ticker"] for w in watch} | {x["ticker"] for x in excluded} \
        | {r["ticker"] for r in latest["pumps"]} | {r["ticker"] for r in wl_rows} | {e["ticker"] for e in ledger[-600:]}
    shown |= {p["ticker"] for p in d_good + d_weak}
    latest["names"] = {tk: kname(tk) for tk in sorted(shown) if kname(tk)}
    for group in (picks, watch, excluded, latest["pumps"], wl_rows, d_good):
        for row in group:
            row["name"] = kname(row["ticker"])
    ledger = ledger[-600:]
    stats = compute_stats(ledger)
    store.write("latest.json", latest, f"스캔 {latest['generated_at']}")
    store.write("ledger.json", ledger, "장부 갱신")
    store.write("stats.json", stats, "통계 갱신")

    log(f"✅ 추천 {len(picks)}건 / 감시 {len(watch)}건 / 제외 {len(excluded)}건 — {time.time() - t0:.0f}초")
    slack_scan(latest, events)
    slack_div4h(d_fresh)
    sector_alert(store, latest["sectors"], at)
    return latest


def run_track():
    store = Store()
    at = now_ms()
    ledger = store.read("ledger.json", [])
    if not isinstance(ledger, list):
        ledger = []
    KR_NAMES.update(store.read("names.json", {}) or {})
    if not KR_NAMES:
        fetch_korean_names(store)
    events = track_ledger(ledger, at) if ledger else []
    # 매시간: 일봉 상승추세 종목(하루 3번 스캔에서 저장)의 4시간 다이버전스 확인
    latest = store.read("latest.json", {}) or {}
    up_list = latest.get("uptrend") or []
    fresh = []
    tickers = fetch_tickers_all() or {}
    smap = store.read("sector_map.json", {}) or {}
    if tickers and smap:
        srows = sector_heat(tickers, smap)
        store.write("sectors_now.json", {"checked_at": iso_kst(at), "sectors": srows}, "섹터 온도")
        sector_alert(store, srows, at)
        log(f"🔥 섹터 온도: 뜨거운 섹터 {[r['group'] for r in srows if r['hot']]}")
    if up_list and tickers:
        good, weak = scan_div4h(up_list, at, tickers, max_age_bars=1)
        fresh = record_div4h(store, good, weak, ledger, at, (latest.get("market") or {}).get("gate", "UNKNOWN"))
        log(f"⚡ 4시간 다이버전스 확인 {len(up_list)}종목 — 거래량 붙음 {len(good)} / 안 붙음 {len(weak)} / 새 신호 {len(fresh)}")
    store.write("ledger.json", ledger, f"장부 갱신 {iso_kst(at)}")
    store.write("stats.json", compute_stats(ledger), "통계 갱신")
    log(f"✅ 장부 갱신 — 이벤트 {len(events)}건")
    slack_div4h(fresh)
    if events:
        slack_post("📣 추천 장부 업데이트", [{"type": "section", "text": {"type": "mrkdwn", "text": "📣 *추천 장부 업데이트*\n" + "\n".join(f"• {x}" for x in events)}}])


# ==========================================================
# 10. 슬랙
# ==========================================================
def slack_post(text: str, blocks: list, thread_ts: Optional[str] = None) -> Optional[str]:
    """메시지를 보내고 ts(스레드 댓글용)를 돌려준다. 실패하면 None."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        return None
    body = {"channel": SLACK_CHANNEL_ID, "text": text, "blocks": blocks[:50], "unfurl_links": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        r = requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, json=body, timeout=15)
        j = r.json()
        if not j.get("ok"):
            log(f"  ⚠️ 슬랙 오류: {j.get('error')}")
            return None
        return j.get("ts")
    except Exception as e:
        log(f"  ⚠️ 슬랙 전송 실패: {e}")
        return None


def _md(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def _ctx(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def _short(text: str, n: int = 70) -> str:
    """첫 문장만, 최대 n자."""
    t = re.split(r"(?<=[.!?다])\s", (text or "").strip())[0]
    return t if len(t) <= n else t[:n - 1] + "…"


def _pick_blocks(p: dict) -> list:
    """종목 하나 = 제목 줄 + 가격 4칸 + 근거 한 줄."""
    nm = kname(p["ticker"])
    title = f"*{nm}* `{p['ticker']}`" if nm else f"*{p['ticker']}*"
    if p["state"] == "now":
        action = f"현재 *{fmt_krw(p['price'])}*  ({p['chg24']:+.1f}%)"
    else:
        gap = (p["price"] / p["zone_hi"] - 1) * 100
        action = f"현재 *{fmt_krw(p['price'])}*  →  *{fmt_krw(p['zone_hi'])} 이하로 오면 매수* (-{gap:.1f}%)"
    ai = p.get("ai") or {}
    flag = {"block": "  ⛔ 심각한 악재", "caution": "  ⚠️ 악재 주의"}.get(ai.get("status"), "")
    blocks = [
        _md(f"{title}  ·  {p['setup_label']}{flag}\n{action}"),
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*매수 구간*\n{fmt_krw(p['zone_lo'])} ~ {fmt_krw(p['zone_hi'])}"},
            {"type": "mrkdwn", "text": f"*손절*\n{fmt_krw(p['stop'])}  (-{p['stop_pct']:.1f}%)"},
            {"type": "mrkdwn", "text": f"*1차 목표* (절반 매도)\n{fmt_krw(p['t1'])}  (+{p['t1_pct']:.1f}%)"},
            {"type": "mrkdwn", "text": f"*2차 목표*\n{fmt_krw(p['t2'])}  (+{p['t2_pct']:.1f}%)"},
        ]},
    ]
    why = p["reasons"][1] if len(p["reasons"]) > 1 else p["reasons"][0]
    line = f"📈 {why}"
    if flag:
        line += f"\n{flag.strip()}: {_short(ai.get('note'), 55)}  _(자세히는 스레드)_"
    blocks.append(_ctx(line))
    return blocks


def slack_scan(latest: dict, events: list):
    m = latest["market"]
    icon = {"GO": "🟢", "SELECTIVE": "🟡", "STOP": "🔴"}.get(m["gate"], "⚪")
    when = latest["generated_at"][5:16].replace("T", " ").replace("-", "/")
    picks = latest["picks"]
    now = [p for p in picks if p["state"] == "now"]
    wait = [p for p in picks if p["state"] != "now"]

    # ── 본문: 신호등 + 추천만 ──
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{icon} {m['label']} · 추천 {len(picks)}개", "emoji": True}},
        _ctx(f"{when} · 2~5일 스윙 · 권장 비중 ×{m['size_mult']:g} · {m['headline']}"),
    ]
    if not picks:
        blocks.append(_md("오늘은 조건을 충족한 종목이 없습니다. 억지로 사지 않는 것도 전략입니다."))
    for head, group in (("✅ 지금 매수 구간", now), ("⏳ 지정가 대기", wait)):
        if not group:
            continue
        blocks.append({"type": "divider"})
        blocks.append(_md(f"*{head}*  ({len(group)})"))
        for p in group:
            blocks += _pick_blocks(p)
    blocks.append({"type": "divider"})
    blocks.append(_ctx(f"<{DASHBOARD_URL}|대시보드에서 실시간 가격 보기> · 감시 후보·AI 설명은 스레드에 · 투자 판단과 책임은 본인에게 있습니다"))
    ts = slack_post(f"{icon} {m['label']} — 추천 {len(picks)}개", blocks)
    if not ts:
        return

    # ── 스레드: 자세한 내용 ──
    th = []
    ai_lines = []
    for p in picks:
        ai = p.get("ai") or {}
        nm = label(p["ticker"])
        if ai.get("status") in ("block", "caution") and ai.get("note"):
            head = "⛔ 심각한 악재" if ai["status"] == "block" else "⚠️ 악재 주의"
            ai_lines.append(f"*{nm}* — {head}\n{ai['note']}" + (f"\n_{ai['recheck']}_" if ai.get("recheck") else ""))
        if ai.get("positive"):
            ai_lines.append(f"*{nm}* — 💡 호재\n{ai['positive']}")
    if ai_lines:
        th.append(_md("🤖 *AI 뉴스 확인* (검색 요약이므로 원문을 확인하세요)"))
        th += [_md(x) for x in ai_lines]
    near = latest["watch"][:8]
    if near:
        rows = []
        for w in near:
            d = f"{w['dist_pct']:+.1f}%" if w.get("dist_pct") is not None else ""
            rows.append(f"• *{label(w['ticker'])}* · {w['setup_label']} · 기준가 {fmt_krw(w['trigger_price'])} ({d})")
        th.append(_md("👀 *감시 후보* — 조건이 맞으면 추천으로 올라옵니다\n" + "\n".join(rows)))
    pumps = latest.get("pumps") or []
    if pumps:
        th.append(_md("🚀 *최근 7일 급등 종목* — 매수 신호 아님, 관찰용\n" + "\n".join(
            f"• *{label(x['ticker'])}* 급등 +{x['pump_pct']:.0f}% → 고점 대비 {x['from_peak_pct']:+.0f}%" for x in pumps[:6])))
    if latest.get("excluded"):
        th.append(_md("🚫 *규칙으로 제외*\n" + "\n".join(f"• *{label(x['ticker'])}* — {x['reason']}" for x in latest["excluded"][:5])))
    if events:
        th.append(_md("📣 *지난 추천 진행 상황*\n" + "\n".join(f"• {x}" for x in events[:10])))
    f = latest.get("funnel")
    chk = " · ".join(("✅ " if c["ok"] else "⚠️ ") + c["text"] for c in m.get("checks", []))
    tail = []
    if chk:
        tail.append(f"🚦 {chk}")
    if f:
        tail.append(f"🔎 스캔 {f['scanned']} → 상승추세 {f['uptrend']} → 눌림 {f['uptrend_pulled_back']} → 매수 조건 {f['picks']}")
    if latest.get("ai_calls_today") is not None:
        tail.append(f"🤖 AI 호출 오늘 {latest['ai_calls_today']}회")
    if tail:
        th.append(_ctx("\n".join(tail)))
    for i in range(0, len(th), 45):
        slack_post("상세", th[i:i + 45], thread_ts=ts)


# ==========================================================
# 11. 백테스트 (일봉 기반 · 생존 편향 있음)
# ==========================================================
def backtest_coin(ticker: str, d: dict, btc_flag: Optional[dict] = None, step: int = 1) -> list:
    """과거 각 날짜를 '그날 종가 시점'으로 되감아 셋업을 찾고 이후 일봉으로 채점."""
    trades = []
    n = len(d["c"])
    busy_until = {}
    for t in range(MIN_BARS + 5, n - 2, step):
        sub = {k: v[:t + 1] for k, v in d.items()}
        cx = {"ticker": ticker, "cur": float(sub["c"][-1]), "chg24": 0.0, "value24": float(sub["c"][-1] * sub["v"][-1]),
              "d": sub, "h1": None}
        found = [r for r in (detect_pullback(cx, None), detect_breakout_retest(cx, None)) if r and r["state"] in ("now", "wait")]
        if not found:
            continue
        best = max(found, key=lambda r: r["score"])
        if t < busy_until.get(best["setup"], -1):
            continue
        p = best["plan"]
        issued = float(d["t"][t] + DAY_MS)
        pick = {"ticker": ticker, "setup": best["setup"], "state": best["state"], "price": cx["cur"], "entry": p["entry"],
                "zone_lo": p["zone_lo"], "zone_hi": p["zone_hi"], "stop": p["stop"], "t1": p["t1"], "t2": p["t2"], "score": best["score"]}
        e = new_entry(pick, int(issued), "BTC>20일선" if (btc_flag or {}).get(float(d["t"][t]), None) else "BTC<20일선" if btc_flag else "n/a")
        e["cursor_ts"] = int(issued)
        e["valid_until_ts"] = int(issued) + (0 if best["state"] == "now" else 2 * DAY_MS)
        fut = {k: v[t + 1:] for k, v in d.items()}
        replay_entry(e, fut, issued + 10 * DAY_MS * 100, bar_ms=DAY_MS, max_hold_ms=5 * DAY_MS)
        if e["status"] in ("pending", "open"):
            continue                                    # 아직 결론 안 난 최신 신호는 제외
        busy_until[best["setup"]] = t + 5
        e["date"] = datetime.fromtimestamp(d["t"][t] / 1000, KST).strftime("%Y-%m-%d")
        trades.append(e)
    return trades


def run_backtest(args: list):
    log("🔬 백테스트 — 일봉 기준(같은 날 손절·목표 동시 도달 시 손절 우선), 수수료 반영")
    tickers = fetch_tickers_all()
    if not tickers:
        raise SystemExit("❌ 빗썸 시세 수집 실패")
    names = [a.upper() for a in args] or [tk for tk, _ in sorted(
        ((tk, v) for tk, v in tickers.items() if tk not in STABLES and tk != "BTC"), key=lambda x: -x[1]["value"])][:30]
    btc = fetch_candles("BTC", "24h", None)
    btc_flag = {}
    if btc:
        e20 = ema(btc["c"], 20)
        btc_flag = {float(btc["t"][i]): bool(btc["c"][i] > e20[i]) for i in range(len(btc["t"])) if not np.isnan(e20[i])}
    trades = []
    for tk in names:
        d = fetch_candles(tk, "24h", None)
        if not d or len(d["c"]) < MIN_BARS + 20:
            log(f"  - {tk}: 데이터 부족")
            continue
        d = {k: v[:-1] for k, v in d.items()}           # 진행 중인 오늘 봉 제외
        tr = backtest_coin(tk, d, btc_flag)
        log(f"  - {tk}: {len(d['c'])}일 / 신호 {len(tr)}건")
        trades += tr
    lines = [f"종목 {len(names)}개 · 신호 {len(trades)}건 · 수수료 편도 {FEE_PCT_PER_SIDE}%",
             "※ 현재 거래대금 상위 종목만 대상이라 '살아남은 종목' 편향이 있어 실제보다 좋게 나올 수 있음", ""]

    def row(label, rows):
        a = _agg(rows)
        if not a["n_closed"]:
            return f"{label:<22} 체결/종료 없음 (신호 {a['n_signals']})"
        return (f"{label:<22} 신호 {a['n_signals']:>3} · 체결률 {a['fill_rate'] or 0:>5.1f}% · 종료 {a['n_closed']:>3} · "
                f"승률 {a['win_rate']:>5.1f}% · 평균 {a['avg_r']:+.2f}R · PF {a['profit_factor'] or 0:.2f}")

    lines.append(row("전체", trades))
    for s in sorted({e["setup"] for e in trades}):
        lines.append(row(f"  {SETUP_LABEL.get(s, s)}", [e for e in trades if e["setup"] == s]))
    for g in sorted({e["gate"] for e in trades}):
        lines.append(row(f"  {g}", [e for e in trades if e["gate"] == g]))
    closed = [e for e in trades if e["status"] == "closed"]
    if len(closed) < 30:
        lines.append("")
        lines.append(f"⚠️ 종료된 거래가 {len(closed)}건뿐이라 결론을 내리기엔 표본이 부족합니다.")
    report = "\n".join(lines)
    log("\n" + report)
    if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        slack_post("백테스트 결과", [{"type": "section", "text": {"type": "mrkdwn", "text": "🔬 *백테스트 결과*\n```" + report[:2800] + "```"}}])


# ==========================================================
# 12. 급등 후 눌림 (pump → dip) 연구 + 실시간 관찰
# ==========================================================
PUMP_MIN_PCT = float(ENV("PUMP_MIN_PCT", "30"))   # 급등 기준: 직전 3일 종가 저점 대비 고가 +30% 이상
PUMP_LOOKBACK = 3                                  # 급등을 만든 기간(일)
PUMP_WATCH_DAYS = 7                                # 고점 이후 관찰 기간
PUMP_DIP_LEVELS = (15, 20, 25, 30)                 # 고점 대비 하락 매수 레벨(%)
PUMP_PATH_DAYS = 12                                # 연구용으로 저장할 고점 이후 일봉 수


def pump_at(d: dict, k: int) -> Optional[dict]:
    """k일 고가가 직전 PUMP_LOOKBACK일 종가 저점 대비 PUMP_MIN_PCT% 이상이면 급등 이벤트."""
    if k < 30 or k >= len(d["c"]):
        return None
    base = float(np.min(d["c"][k - PUMP_LOOKBACK:k]))
    if base <= 0:
        return None
    pct = (float(d["h"][k]) / base - 1) * 100
    if pct < PUMP_MIN_PCT:
        return None
    val20 = float(np.mean((d["c"] * d["v"])[k - 20 - PUMP_LOOKBACK:k - PUMP_LOOKBACK])) if k >= 20 + PUMP_LOOKBACK else None
    return {"k": k, "base": base, "pump_pct": round(pct, 1), "pre_value_krw": val20}


def collect_pump_events(ticker: str, d: dict, since_ms: float = 0) -> list:
    """과거 급등 이벤트와 고점 이후 일봉 경로(고점 대비 비율)를 수집. 같은 급등은 고점 기준 하나로 합친다."""
    out = []
    n = len(d["c"])
    k = 30
    while k < n:
        ev = pump_at(d, k)
        if not ev or d["t"][k] < since_ms:
            k += 1
            continue
        # 급등이 이어지는 동안 고점 갱신 (최대 PUMP_LOOKBACK+2일)
        peak_k = k
        for j in range(k + 1, min(n, k + PUMP_LOOKBACK + 3)):
            if d["h"][j] > d["h"][peak_k]:
                peak_k = j
            else:
                break
        H = float(d["h"][peak_k])
        path = []
        for j in range(peak_k + 1, min(n, peak_k + 1 + PUMP_PATH_DAYS)):
            path.append([round(float(d[x][j]) / H, 5) for x in ("o", "h", "l", "c")])
        pre_c = [round(float(x) / H, 5) for x in d["c"][max(0, k - 10):peak_k + 1]]
        vol_ratio = float(d["v"][peak_k] / np.mean(d["v"][max(0, k - 20):k])) if k >= 5 and np.mean(d["v"][max(0, k - 20):k]) > 0 else None
        out.append({"ticker": ticker, "date": datetime.fromtimestamp(d["t"][peak_k] / 1000, KST).strftime("%Y-%m-%d"),
                    "peak": H, "base": ev["base"], "pump_pct": round((H / ev["base"] - 1) * 100, 1),
                    "pump_days": peak_k - k + 1, "pre_value_krw": ev["pre_value_krw"],
                    "peak_close_ratio": round(float(d["c"][peak_k]) / H, 4), "vol_ratio": round(vol_ratio, 1) if vol_ratio else None,
                    "pre_closes": pre_c, "path": path})
        k = peak_k + PUMP_WATCH_DAYS                     # 같은 급등의 중복 집계 방지
    return out


def sim_pump_dip(ev: dict, dip: float, stop: Optional[float], target: Optional[float], hold: int = 5,
                 wait: int = PUMP_WATCH_DAYS, fee_pct: float = None) -> Optional[dict]:
    """고점 대비 dip% 하락 지정가 매수 → 손절 stop%, 목표 target%, hold일 후 종가 청산.
    일봉만 있으므로 같은 날 손절·목표 동시 가능 시 손절, 체결일엔 목표 미인정(보수적)."""
    fee = (FEE_PCT_PER_SIDE if fee_pct is None else fee_pct) * 2
    path = ev["path"]
    peak = 1.0
    entry_i, fill = None, None
    for i, (o, h, l, c) in enumerate(path[:wait]):
        lvl = peak * (1 - dip / 100)
        if l <= lvl:
            entry_i, fill = i, min(o, lvl)
            break
        peak = max(peak, h)
    if entry_i is None:
        return None
    sp = fill * (1 - stop / 100) if stop else None
    tp = fill * (1 + target / 100) if target else None
    exit_px, why = None, None
    o, h, l, c = path[entry_i]
    if sp and l <= sp:
        exit_px, why = min(sp, fill), "stop"
    else:
        end = min(len(path), entry_i + 1 + hold)
        for j in range(entry_i + 1, end):
            o, h, l, c = path[j]
            if sp and l <= sp:
                exit_px, why = min(sp, o), "stop"
                break
            if tp and h >= tp:
                exit_px, why = max(tp, o), "target"
                break
        if exit_px is None:
            if end - (entry_i + 1) < hold:
                return None                             # 데이터 끝 — 결론 안 남
            exit_px, why = path[end - 1][3], "time"
    j_last = path[entry_i + 1:entry_i + 1 + hold]
    mfe = (max([x[1] for x in j_last] or [fill]) / fill - 1) * 100
    mae = (min([x[2] for x in path[entry_i:entry_i + 1 + hold]]) / fill - 1) * 100
    return {"ret": (exit_px / fill - 1) * 100 - fee, "why": why, "days_to_fill": entry_i + 1,
            "below_base": fill < ev["base"] / ev["peak"], "mfe": mfe, "mae": mae}


def summarize_pump(events: list) -> dict:
    def agg(rs):
        if not rs:
            return None
        v = [r["ret"] for r in rs]
        w = [x for x in v if x > 0]
        l = [x for x in v if x <= 0]
        return {"n": len(v), "win": round(len(w) / len(v) * 100, 1), "avg": round(sum(v) / len(v), 2),
                "median": round(statistics.median(v), 2), "pf": round(sum(w) / -sum(l), 2) if sum(l) < 0 else None,
                "stop_rate": round(sum(r["why"] == "stop" for r in rs) / len(rs) * 100, 1)}
    grid = []
    for dip in PUMP_DIP_LEVELS:
        for stop in (8, 12, 15, None):
            for target in (8, 12, 20, None):
                for hold in (2, 5):
                    rs = [x for x in (sim_pump_dip(e, dip, stop, target, hold) for e in events) if x]
                    a = agg(rs)
                    if a:
                        grid.append({"dip": dip, "stop": stop, "target": target, "hold": hold, **a})
    # 고점 이후 행동 기초 통계
    reach = {}
    for dip in (15, 20, 25, 30, 40, 50):
        hits = sum(1 for e in events if any(p[2] <= 1 - dip / 100 for p in e["path"][:PUMP_WATCH_DAYS]))
        reach[f"-{dip}%"] = round(hits / len(events) * 100, 1) if events else None
    back_to_base = sum(1 for e in events if any(p[2] <= e["base"] / e["peak"] for p in e["path"][:PUMP_WATCH_DAYS]))
    return {"events": len(events), "tickers": len({e["ticker"] for e in events}),
            "reach_within_7d": reach,
            "back_to_base_within_7d_pct": round(back_to_base / len(events) * 100, 1) if events else None,
            "grid": sorted(grid, key=lambda g: -g["avg"])}


def run_pumpdip(args: list):
    days = int(args[0]) if args and args[0].isdigit() else 730
    log(f"🔬 급등 후 눌림 연구 — 최근 {days}일, 빗썸 원화마켓 전 종목 (상장폐지 종목은 빠져 있음 → 실제보다 좋게 나올 수 있음)")
    tickers = fetch_tickers_all()
    if not tickers:
        raise SystemExit("❌ 빗썸 시세 수집 실패")
    names = sorted(tk for tk in tickers if tk not in STABLES)
    since = now_ms() - days * DAY_MS
    events = []

    def one(tk):
        d = fetch_candles(tk, "24h", None)
        if not d or len(d["c"]) < 40:
            return []
        d = slice_completed(d, DAY_MS, now_ms())
        return collect_pump_events(tk, d, since)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(one, tk) for tk in names]):
            try:
                events += f.result()
            except Exception as e:
                log(f"  ⚠️ {e}")
    events.sort(key=lambda e: e["date"])
    log(f"  ✅ {len(names)}종 스캔, 급등 이벤트 {len(events)}건")
    summary = summarize_pump(events)
    store = Store()
    store.write("pumpdip_events.json", {"generated_at": iso_kst(now_ms()), "days": days, "pump_min_pct": PUMP_MIN_PCT,
                                        "fee_pct_per_side": FEE_PCT_PER_SIDE, "events": events}, "급등 눌림 이벤트")
    store.write("pumpdip_summary.json", {"generated_at": iso_kst(now_ms()), **summary}, "급등 눌림 요약")
    lines = [f"이벤트 {summary['events']}건 / {summary['tickers']}종목 (급등 기준 +{PUMP_MIN_PCT:.0f}%)",
             "고점 이후 7일 안에 도달한 하락폭: " + ", ".join(f"{k} {v}%" for k, v in summary["reach_within_7d"].items()),
             f"급등 전 가격까지 되돌림: {summary['back_to_base_within_7d_pct']}%", "", "상위 조합 (평균 수익 순, n≥30):"]
    for g in [g for g in summary["grid"] if g["n"] >= 30][:8]:
        lines.append(f"  -{g['dip']}% 매수 · 손절 {g['stop'] or '없음'} · 목표 {g['target'] or '없음'} · {g['hold']}일 → "
                     f"n={g['n']} 승률 {g['win']}% 평균 {g['avg']:+.2f}% PF {g['pf']}")
    report = "\n".join(lines)
    log("\n" + report)
    slack_post("급등 후 눌림 연구", [{"type": "section", "text": {"type": "mrkdwn", "text": "🔬 *급등 후 눌림 연구*\n```" + report[:2800] + "```"}}])


def live_pumps(ctxs: dict) -> list:
    """최근 7일 안에 +30% 이상 급등한 종목과 고점 대비 현재 위치 (관찰용)."""
    rows = []
    for tk, cx in ctxs.items():
        d = cx["d"]
        n = len(d["c"])
        if n < 40:
            continue
        best = None
        for k in range(max(30, n - PUMP_WATCH_DAYS), n):
            ev = pump_at(d, k)
            if ev and (best is None or d["h"][k] > d["h"][best["k"]]):
                best = ev
        if not best:
            continue
        H = float(max(np.max(d["h"][best["k"]:]), cx["cur"]))
        cur = cx["cur"]
        rows.append({"ticker": tk, "price": cur, "peak": rp(H), "base": rp(best["base"]),
                     "pump_pct": round((H / best["base"] - 1) * 100, 1),
                     "from_peak_pct": round((cur / H - 1) * 100, 1),
                     "days_since_peak": int(n - 1 - (best["k"] + int(np.argmax(d["h"][best["k"]:])))),
                     "levels": {str(x): rp(H * (1 - x / 100)) for x in PUMP_DIP_LEVELS},
                     "below_base": bool(cur < best["base"]), "value24": cx["value24"]})
    rows.sort(key=lambda r: r["from_peak_pct"])
    return rows[:15]


# ==========================================================
# 13. 시세 스냅샷 (연구용: 전 종목 일봉 60개 + 1시간봉 120개)
# ==========================================================
def run_snapshot(args: list):
    at = now_ms()
    tickers = fetch_tickers_all()
    if not tickers:
        raise SystemExit("❌ 빗썸 시세 수집 실패")
    fetch_korean_names(Store())
    uni = build_universe(tickers)
    out = {}

    def one(tk):
        d = fetch_candles(tk, "24h", None)
        h = fetch_candles(tk, "1h", 121)
        return tk, d, h

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(one, tk) for tk in uni]):
            try:
                tk, d, h = f.result()
            except Exception:
                continue
            if not d:
                continue
            row = {"name": kname(tk), "price": tickers[tk]["price"], "chg24": tickers[tk]["chg"], "value24": tickers[tk]["value"],
                   "d": [[int(d["t"][i]), d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["v"][i]] for i in range(len(d["t"]))]}
            if h:
                row["h"] = [[int(h["t"][i]), h["o"][i], h["h"][i], h["l"][i], h["c"][i], h["v"][i]] for i in range(len(h["t"]))]
            out[tk] = row
    Store().write("snapshot.json", {"generated_at": iso_kst(at), "generated_ts": at, "coins": out}, "시세 스냅샷")
    log(f"✅ 스냅샷 {len(out)}종목")


# ==========================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "scan"
    if cmd == "scan":
        run_scan()
    elif cmd == "track":
        run_track()
    elif cmd == "backtest":
        run_backtest(argv[1:])
    elif cmd == "pumpdip":
        run_pumpdip(argv[1:])
    elif cmd == "snapshot":
        run_snapshot(argv[1:])
    else:
        raise SystemExit("사용법: python bot.py [scan|track|backtest 티커...]")


if __name__ == "__main__":
    main()
