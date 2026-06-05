import time
import json
import re
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz
import requests
import numpy as np
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ==========================================
# ⚙️ 설정값
# ==========================================
import os

# ==========================================
# ⚙️ 설정값 (GitHub Actions 환경변수 또는 직접 입력)
# ==========================================
SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN_BOT", "")
WATCHLIST        = [t.strip().upper() for t in os.environ.get("WATCHLIST", "").split(",") if t.strip()]
GITHUB_REPO      = "https://github.com/insight-doby/report_coin.git"

INTERVALS = {
    "30m": {"limit": 100, "label": "30분봉"},
    "1h":  {"limit": 100, "label": "1시간봉"},
    "6h":  {"limit": 100, "label": "6시간봉"},
    "1d":  {"limit": 200, "label": "일봉"},
    "1w":  {"limit": 60,  "label": "주봉"},
    "1M":  {"limit": 24,  "label": "월봉"},
}

SCAN_TOP_N           = 80
TOP_N_FOR_INDICATORS = 30
CUTOFF_RANK          = 30
TOP30_MAJOR_N        = 5   # TOP30 메이저 알트에서 지표 수집할 종목 수

# 시장 온도계용 메이저 코인 (거래대금 비중 계산)
MAJOR_TICKERS = {
    "BTC","ETH","XRP","SOL","BNB","USDT","USDC",
    "ADA","AVAX","DOT","MATIC","LINK","TRX","LTC","BCH"
}

# TOP30 스캔에서 제외할 스테이블코인 및 무의미 토큰
STABLE_EXCLUDE = {
    "USDT","USDC","BUSD","DAI","TUSD","USDP","GUSD","FRAX",
    "UST","LUSD","SUSD","USDD","PYUSD","FDUSD","WBTC","WETH",
}

KST = pytz.timezone("Asia/Seoul")


# ==========================================
# ── BTC 동조화 지수 계산
# ==========================================
def calc_btc_correlation(ticker: str, btc_closes_1h: np.ndarray, btc_vols_1h: np.ndarray) -> dict:
    result = {
        "beta_4h":        None,
        "corr_24h":       None,
        "defense_label":  "계산불가",
        "defense_detail": "",
        "vol_div_label":  "계산불가",
        "vol_div_detail": "",
        "btc_ma_state":   "계산불가",
        "alt_ma_state":   "계산불가",
        "beta_label":     "계산불가",
    }
    try:
        alt_url = f"https://api.binance.com/api/v3/klines?symbol={ticker}USDT&interval=1h&limit=30"
        r = requests.get(alt_url, timeout=8)
        if r.status_code != 200:
            return result
        alt_klines = r.json()
        if len(alt_klines) < 6:
            return result

        alt_closes = np.array([float(k[4]) for k in alt_klines])
        alt_vols   = np.array([float(k[5]) for k in alt_klines])

        n = min(len(alt_closes), len(btc_closes_1h))
        alt_c = alt_closes[-n:]
        btc_c = btc_closes_1h[-n:]

        # 4시간 베타
        if n >= 5:
            alt_ret_4h = (alt_c[-1] - alt_c[-5]) / alt_c[-5] * 100
            btc_ret_4h = (btc_c[-1] - btc_c[-5]) / btc_c[-5] * 100
            if abs(btc_ret_4h) > 0.01:
                beta = round(alt_ret_4h / btc_ret_4h, 2)
                result["beta_4h"] = beta
                if   beta < 0.3:  result["beta_label"] = "🟢 독자 흐름 (BTC 비연동)"
                elif beta < 0.8:  result["beta_label"] = "🟡 약한 동조화"
                elif beta < 1.2:  result["beta_label"] = "🟠 평균 동조화"
                else:             result["beta_label"] = "🔴 강한 동조화 (BTC 종속)"
            else:
                result["beta_label"] = "🟡 BTC 횡보 중 (측정 불가)"

        # 24시간 상관계수
        if n >= 25:
            alt_ret = np.diff(alt_c[-25:]) / alt_c[-25:-1]
            btc_ret = np.diff(btc_c[-25:]) / btc_c[-25:-1]
            if np.std(alt_ret) > 0 and np.std(btc_ret) > 0:
                corr = float(np.corrcoef(alt_ret, btc_ret)[0, 1])
                result["corr_24h"] = round(corr, 3)

        # BTC 하락 구간 방어력
        if n >= 5:
            btc_rets_4 = [(btc_c[-(5-i)] - btc_c[-(6-i)]) / btc_c[-(6-i)] * 100
                          for i in range(4)]
            worst_idx  = int(np.argmin(btc_rets_4))
            btc_worst  = round(btc_rets_4[worst_idx], 2)

            if btc_worst < -0.3:
                alt_rets_4 = [(alt_c[-(5-i)] - alt_c[-(6-i)]) / alt_c[-(6-i)] * 100
                              for i in range(4)]
                alt_at_worst = round(alt_rets_4[worst_idx], 2)
                ratio = alt_at_worst / btc_worst if btc_worst != 0 else 0
                if   alt_at_worst >= 0:
                    result["defense_label"]  = "🛡️ 방어력 [최상] — BTC 하락 시 상승"
                elif ratio < 0.5:
                    result["defense_label"]  = "🟢 방어력 [상] — BTC 대비 절반 이하 하락"
                elif ratio < 1.0:
                    result["defense_label"]  = "🟡 방어력 [중] — BTC와 유사한 하락"
                else:
                    result["defense_label"]  = "🔴 방어력 [하] — BTC보다 더 큰 하락"
                result["defense_detail"] = (
                    f"BTC {btc_worst:+.2f}% 조정 시 본 종목 {alt_at_worst:+.2f}% "
                    f"({'상승 방어' if alt_at_worst >= 0 else '하락 ' + str(abs(alt_at_worst)) + '%'})"
                )
            else:
                result["defense_label"]  = "⚪ 측정 불가 (BTC 횡보 구간)"
                result["defense_detail"] = "최근 4시간 BTC 유의미한 하락 없음"

        # 거래량 다이버전스
        alt_v = alt_vols[-n:]
        btc_v = btc_vols_1h[-n:]
        if n >= 14:
            alt_vol_recent = float(np.mean(alt_v[-3:]))
            alt_vol_base   = float(np.mean(alt_v[-13:-3]))
            btc_vol_recent = float(np.mean(btc_v[-3:]))
            btc_vol_base   = float(np.mean(btc_v[-13:-3]))

            alt_vol_ratio  = round(alt_vol_recent / alt_vol_base * 100, 1) if alt_vol_base > 0 else 0
            btc_vol_ratio  = round(btc_vol_recent / btc_vol_base * 100, 1) if btc_vol_base > 0 else 0

            if alt_vol_ratio >= 200 and btc_vol_ratio < 120:
                result["vol_div_label"]  = "🚨 거래량 다이버전스 [강] — 독자 수급 발생"
            elif alt_vol_ratio >= 150 and btc_vol_ratio < 130:
                result["vol_div_label"]  = "📈 거래량 다이버전스 [중] — 수급 유입 감지"
            elif alt_vol_ratio < 80 and btc_vol_ratio >= 120:
                result["vol_div_label"]  = "📉 역방향 다이버전스 — 수급 이탈 주의"
            else:
                result["vol_div_label"]  = "⚖️ 동반 변동 — 독자 수급 없음"

            result["vol_div_detail"] = (
                f"본 종목 거래량 평균 대비 {alt_vol_ratio}% "
                f"/ BTC 거래량 {btc_vol_ratio}%"
            )

        # EMA20 상태 비교
        if n >= 21:
            def ema20(arr):
                k, e = 2/21, arr[0]
                for p in arr[1:]: e = p*k + e*(1-k)
                return e

            alt_ema20 = ema20(alt_c[-21:])
            btc_ema20 = ema20(btc_c[-21:])
            result["alt_ma_state"] = "EMA20 위 (정배열)" if alt_c[-1] > alt_ema20 else "EMA20 아래 (역배열)"
            result["btc_ma_state"] = "EMA20 위 (정배열)" if btc_c[-1] > btc_ema20 else "EMA20 아래 (역배열)"

    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_btc_1h_base() -> tuple:
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=35"
        r   = requests.get(url, timeout=8)
        if r.status_code == 200:
            klines = r.json()
            closes = np.array([float(k[4]) for k in klines])
            vols   = np.array([float(k[5]) for k in klines])
            return closes, vols
    except Exception as e:
        print(f"  ⚠️ BTC 1h 기준 데이터 수집 실패: {e}")
    return np.array([]), np.array([])


# ==========================================
# ── 김프(KRW 프리미엄) 계산
# ==========================================
def calc_kimchi_premium(ticker: str, krw_price: float, usd_krw_rate: float = 1380.0) -> dict:
    result = {"kimp_pct": None, "kimp_label": "계산불가", "binance_krw": None}
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={ticker}USDT"
        r   = requests.get(url, timeout=5)
        if r.status_code == 200:
            usdt_price  = float(r.json()["price"])
            binance_krw = usdt_price * usd_krw_rate
            kimp_pct    = round((krw_price / binance_krw - 1) * 100, 2)
            result["kimp_pct"]    = kimp_pct
            result["binance_krw"] = round(binance_krw, 0)

            if   kimp_pct >= 5:    result["kimp_label"] = f"🔴 고김프 +{kimp_pct}% (과매수 주의)"
            elif kimp_pct >= 2:    result["kimp_label"] = f"🟡 양김프 +{kimp_pct}% (국내 수요 강함)"
            elif kimp_pct >= -1:   result["kimp_label"] = f"🟢 정상 {kimp_pct:+.2f}%"
            else:                  result["kimp_label"] = f"🔵 역김프 {kimp_pct}% (해외 대비 저평가)"
    except Exception:
        pass
    return result


# ==========================================
# 1-A. 빗썸 시세 수집
# ==========================================
def fetch_rising_star_bithumb_krw_coins():
    print("🔍 빗썸 전체 시세 수집 중 (TOP30 메이저 알트 + TOP31~80 수급급증 필터)...")
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            res = r.json()
            if res.get("status") == "0000":
                coins_data = {}
                for ticker, info in res["data"].items():
                    if ticker == "date" or not isinstance(info, dict):
                        continue
                    try:
                        coins_data[ticker] = {
                            "price":  int(float(info["closing_price"])),
                            "change": float(info["fluctate_rate_24H"]),
                            "volume": float(info["acc_trade_value_24H"]),
                        }
                    except (ValueError, KeyError):
                        continue

                sorted_all = sorted(coins_data.items(), key=lambda x: x[1]["volume"], reverse=True)

                if len(sorted_all) < CUTOFF_RANK + 1:
                    print(f"  ⚠️ 데이터 부족: {len(sorted_all)}개 (최소 {CUTOFF_RANK+1}개 필요)")
                    return [], [], 0, {}

                top_30_cutoff = sorted_all[CUTOFF_RANK - 1][1]["volume"]

                # ── TOP30 메이저 알트 추출 (스테이블·BTC 제외) ──
                major_alt_pool = [
                    (t, v) for t, v in sorted_all[:CUTOFF_RANK]
                    if t not in STABLE_EXCLUDE and t != "BTC"
                ]
                top30_targets = major_alt_pool[:TOP30_MAJOR_N]
                top30_names = ", ".join(f"{t}({i+1}위)" for i, (t, _) in enumerate(top30_targets[:3]))
                print(f"  📌 TOP30 메이저 알트 {len(top30_targets)}종: {top30_names} ...")

                # ── 31~80위 수급 급증 후보 추출 ──
                candidates = sorted_all[CUTOFF_RANK:min(SCAN_TOP_N, len(sorted_all))]
                if not candidates:
                    print("  ⚠️ 후보 코인 없음")
                    return [], [], 0, {}

                vols      = [v["volume"] for _, v in candidates]
                vol_min, vol_max = min(vols), max(vols)
                vol_range = vol_max - vol_min if vol_max > vol_min else 1

                scored = []
                for ticker, info in candidates:
                    vs = (info["volume"] - vol_min) / vol_range
                    cs = abs(info["change"]) / 100
                    scored.append((ticker, info, vs * 0.6 + cs * 0.4))
                scored.sort(key=lambda x: x[2], reverse=True)

                target = [(t, v) for t, v, _ in scored[:TOP_N_FOR_INDICATORS]]
                rank_map = {t: i + CUTOFF_RANK + 1 for i, (t, _) in enumerate(candidates)}
                top5 = ", ".join(f"{t}({rank_map.get(t,'?')}위)" for t, _ in target[:5])
                print(f"✅ 수급급증 후보 {len(candidates)}개 → 선별 {len(target)}개 | 컷오프: {top_30_cutoff:,.0f}원\n"
                      f"   상위 5: {top5} ...")
                return target, top30_targets, top_30_cutoff, coins_data
    except Exception as e:
        print(f"❌ 빗썸 API 오류: {e}")
    return [], [], 0, {}


# ==========================================
# 1-B. 시장 활성도 분석
# ==========================================
def fetch_market_activity(coins_data: dict) -> dict:
    print("📊 시장 활성도 수집 중...")
    total_volume = sum(v["volume"] for v in coins_data.values())
    major_volume = sum(v["volume"] for t, v in coins_data.items() if t in MAJOR_TICKERS)
    alt_volume   = total_volume - major_volume
    major_ratio  = round(major_volume / total_volume * 100, 1) if total_volume > 0 else 0
    alt_ratio    = round(100 - major_ratio, 1)

    major_top5 = sorted(
        [(t, v["volume"], v["change"]) for t, v in coins_data.items() if t in MAJOR_TICKERS],
        key=lambda x: x[1], reverse=True
    )[:5]
    alt_top5 = sorted(
        [(t, v["volume"], v["change"]) for t, v in coins_data.items() if t not in MAJOR_TICKERS],
        key=lambda x: x[1], reverse=True
    )[:5]

    avg_total_krw_estimate = None
    level_label = "알 수 없음"
    vs_avg_pct  = None

    try:
        rc = requests.get("https://api.bithumb.com/public/candlestick/BTC_KRW/24h", timeout=10)
        if rc.status_code == 200:
            rc_json = rc.json()
            if rc_json.get("status") == "0000":
                candles = rc_json["data"]
                recent  = candles[-31:-1] if len(candles) >= 31 else candles[:-1]
                if len(recent) >= 7:
                    btc_30d_avg = float(np.mean([float(c[2]) * float(c[5]) for c in recent]))
                    btc_today   = coins_data.get("BTC", {}).get("volume", 0)
                    btc_share   = (btc_today / total_volume) if btc_today > 0 and total_volume > 0 else 0.40
                    avg_total_krw_estimate = btc_30d_avg / max(btc_share, 0.05)
                    vs_avg_pct = round(total_volume / avg_total_krw_estimate * 100, 1)
                    print(f"  ✅ BTC 30일 평균: {btc_30d_avg/1e8:.0f}억 / 전체 추정 평균: {avg_total_krw_estimate/1e12:.2f}조")

                    if   vs_avg_pct >= 200: level_label = "🚀 과열/광풍 (전월 평균의 2배↑)"
                    elif vs_avg_pct >= 130: level_label = "🔥 활성화 (전월 평균 이상)"
                    elif vs_avg_pct >= 70:  level_label = "😊 보통 (전월 평균 수준)"
                    elif vs_avg_pct >= 40:  level_label = "😴 관망 (전월 평균 이하)"
                    else:                   level_label = "🧊 냉각 (극도로 한산)"
    except Exception as e:
        print(f"  ⚠️ 기준 거래대금 산출 실패: {e}")

    if   major_ratio >= 65: supply = "🔵 메이저 주도장"
    elif alt_ratio   >= 65: supply = "🟠 알트 주도장"
    else:                   supply = "⚖️ 혼조장"

    return {
        "total_volume": total_volume, "major_volume": major_volume, "alt_volume": alt_volume,
        "major_ratio": major_ratio, "alt_ratio": alt_ratio,
        "major_top5": major_top5, "alt_top5": alt_top5,
        "avg_estimate": avg_total_krw_estimate, "vs_avg_pct": vs_avg_pct,
        "level_label": level_label, "supply_character": supply,
    }


# ==========================================
# 2. 기술적 지표 계산 헬퍼
# ==========================================
def _calc_ema(closes: np.ndarray, period: int) -> Optional[float]:
    if len(closes) < period: return None
    k, ema = 2/(period+1), closes[0]
    for p in closes[1:]: ema = p*k + ema*(1-k)
    return round(ema, 6)

def _calc_rsi(closes: np.ndarray, period: int = 14) -> Optional[float]:
    if len(closes) < period+1: return None
    d = np.diff(closes)
    ag = np.mean(np.where(d>0, d, 0)[:period])
    al = np.mean(np.where(d<0, -d, 0)[:period])
    for i in range(period, len(d)):
        ag = (ag*(period-1) + max(d[i], 0)) / period
        al = (al*(period-1) + max(-d[i], 0)) / period
    # ✅ FIX 3: al == 0 일 때 ZeroDivisionError 방지
    if al == 0:
        return 100.0
    return round(100 - 100/(1 + ag/al), 2)

def _calc_bollinger(closes: np.ndarray, period: int = 20):
    if len(closes) < period: return None, None, None
    w = closes[-period:]
    m = float(np.mean(w)); s = float(np.std(w, ddof=1))
    return round(m+2*s, 6), round(m, 6), round(m-2*s, 6)

def _ma_align(closes: np.ndarray, periods: list) -> str:
    emas = {p: _calc_ema(closes, p) for p in periods}
    emas = {p: v for p, v in emas.items() if v is not None}
    if len(emas) < 2: return "계산불가"
    vals = [emas[p] for p in sorted(emas)]
    if all(vals[i] > vals[i+1] for i in range(len(vals)-1)): return "✅ 완전 정배열"
    if all(vals[i] < vals[i+1] for i in range(len(vals)-1)): return "🔴 역배열"
    return "⚠️ 혼조 (수렴 중)"


# ==========================================
# 3. 바이낸스 멀티 타임프레임 지표
# ==========================================
def fetch_binance_indicators(ticker: str, btc_closes_1h: np.ndarray, btc_vols_1h: np.ndarray, krw_price: float) -> dict:
    symbol = ticker + "USDT"
    result = {}
    ma_periods = {
        "30m":[5,20], "1h":[5,20,60], "6h":[20,60,120],
        "1d":[20,60,120,200], "1w":[20,60], "1M":[12,24],
    }
    for interval, cfg in INTERVALS.items():
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={cfg['limit']}"
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                result[interval] = {"error": f"HTTP {resp.status_code}"}; continue
            klines = resp.json()
            if not klines:
                result[interval] = {"error": "데이터 없음"}; continue

            closes = np.array([float(k[4]) for k in klines])
            vols   = np.array([float(k[5]) for k in klines])
            cur    = closes[-1]
            periods = ma_periods.get(interval, [20])
            emas    = {f"EMA{p}": _calc_ema(closes, p) for p in periods}
            bbu, bbm, bbl = _calc_bollinger(closes)
            bb_pos = None
            if bbu and bbl and bbm:
                if cur >= bbm:
                    bb_pos = f"중단 이상 ({round((cur-bbm)/(bbu-bbm)*100,1)}% 위치)"
                else:
                    bb_pos = f"중단 이하 ({round((bbm-cur)/(bbm-bbl)*100,1)}% 위치)"
            vr = float(np.mean(vols[-3:])) / float(np.mean(vols[-13:-3])) * 100 \
                 if len(vols) >= 13 and np.mean(vols[-13:-3]) > 0 else None

            result[interval] = {
                "label": cfg["label"], "current": round(cur, 6),
                "emas": {k: round(v, 6) if v else None for k, v in emas.items()},
                "rsi": _calc_rsi(closes),
                "bb_upper": bbu, "bb_mid": bbm, "bb_lower": bbl, "bb_position": bb_pos,
                "vol_ratio": f"{round(vr,1)}%" if vr else "계산불가",
                "ma_align": _ma_align(closes, periods),
            }
        except Exception as e:
            result[interval] = {"error": str(e)}
        time.sleep(0.05)

    if len(btc_closes_1h) >= 6:
        result["btc_sync"] = calc_btc_correlation(ticker, btc_closes_1h, btc_vols_1h)

    if krw_price > 0:
        result["kimp"] = calc_kimchi_premium(ticker, krw_price)

    return result


def fetch_indicators_for_top_coins(target_coins: list, btc_closes_1h: np.ndarray, btc_vols_1h: np.ndarray) -> dict:
    targets = target_coins[:TOP_N_FOR_INDICATORS]
    total   = len(targets)
    print(f"📊 {total}개 코인 지표 병렬 수집 중...")
    indicators = {}
    completed  = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                fetch_binance_indicators, ticker, btc_closes_1h, btc_vols_1h, info["price"]
            ): ticker
            for ticker, info in targets
        }
        for future in as_completed(futures):
            ticker    = futures[future]
            completed += 1
            try:
                indicators[ticker] = future.result()
                print(f"  ✅ [{completed}/{total}] {ticker} 완료")
            except Exception as e:
                indicators[ticker] = {}
                print(f"  ⚠️ [{completed}/{total}] {ticker} 실패: {e}")
    return indicators


def format_indicators_for_prompt(indicators: dict, target_coins: list) -> str:
    coin_price_map = {t: v["price"] for t, v in target_coins}
    lines = []
    for ticker, ivs in indicators.items():
        krw_price = coin_price_map.get(ticker, 0)
        lines.append(f"\n▶ {ticker} (현재가: {krw_price:,}원)")

        for interval, data in ivs.items():
            if interval in ("btc_sync", "kimp") or "error" in data:
                continue
            lines.append(
                f"  [{data.get('label', interval)}] "
                f"RSI:{data.get('rsi','N/A')} | "
                f"볼밴:{data.get('bb_position','N/A')} | "
                f"이평:{data.get('ma_align','N/A')} | "
                f"거래량:{data.get('vol_ratio','N/A')}"
            )

        sync = ivs.get("btc_sync", {})
        if sync and not sync.get("error"):
            lines.append(
                f"  [BTC동조화] 베타:{sync.get('beta_label','—')} | "
                f"{sync.get('defense_label','—')} | "
                f"{sync.get('vol_div_label','—')}"
            )
            if sync.get("defense_detail"):
                lines.append(f"    └ {sync['defense_detail']}")
            if sync.get("vol_div_detail"):
                lines.append(f"    └ {sync['vol_div_detail']}")
            lines.append(
                f"  [추세역행] BTC:{sync.get('btc_ma_state','—')} / "
                f"본종목:{sync.get('alt_ma_state','—')}"
            )

        kimp = ivs.get("kimp", {})
        if kimp and kimp.get("kimp_pct") is not None:
            lines.append(f"  [김프] {kimp.get('kimp_label','—')}")

    return "\n".join(lines)


# ==========================================
# 4-A. 1호출 — 거시/코인니스 시장 내러티브
# ==========================================
def fetch_market_narrative() -> dict:
    print("📰 [1호출] 시장 내러티브 수집 중 (Gemini + 구글서치)...")
    clean_key = GEMINI_API_KEY.strip()
    if not clean_key:
        return {}

    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")

    prompt = f"""
오늘({today_str}) 암호화폐 시장의 주요 뉴스와 내러티브를 구글서치로 찾아서 아래 JSON 형식으로 정리해라.
코인니스(coinness.com) 기사를 우선으로 참고해라.
반드시 오늘 또는 최근 24시간 이내 뉴스만 사용해라.

검색할 항목:
1. 거시경제: 미국 금리/연준 발언, 나스닥/S&P500 흐름, 달러인덱스(DXY)
2. BTC 특이사항: 고래 움직임, 비트코인 현물 ETF 자금 유출입, 온체인 청산 데이터, MSTR 등 기관 동향
3. 개별 코인 촉매: 오늘 급등/급락한 코인들의 구체적 이유 (프로토콜 업데이트, 파트너십, 언락 등)
4. 섹터 흐름: 오늘 강한 섹터와 약한 섹터 (AI/DeFi/L2/RWA/GameFi 등)
5. 김치프리미엄: 국내 수급 특이사항, 빗썸/업비트 거래량 이슈
6. 규제/정책: SEC/CFTC 동향, 미국 크립토 클래리티 법안, 각국 규제 이슈

출력은 순수 JSON만 반환해라:
{{
  "macro": {{
    "fed": "연준/금리 관련 내용 (없으면 null)",
    "nasdaq": "나스닥/S&P 흐름 (없으면 null)",
    "dxy": "달러인덱스 동향 (없으면 null)",
    "summary": "거시경제 한 줄 요약"
  }},
  "btc": {{
    "whale": "고래 움직임 (없으면 null)",
    "etf_flow": "ETF 자금 유출입 (없으면 null)",
    "liquidation": "청산 데이터 (없으면 null)",
    "institution": "기관 동향 (없으면 null)",
    "summary": "BTC 특이사항 한 줄 요약"
  }},
  "coin_catalysts": [
    {{"ticker": "티커", "direction": "상승 또는 하락", "reason": "구체적 이유", "source": "출처"}}
  ],
  "sectors": {{
    "hot": [{{"name": "섹터명", "reason": "강한 이유"}}],
    "cold": [{{"name": "섹터명", "reason": "약한 이유"}}],
    "dominant_theme": "오늘 시장을 이끄는 핵심 테마 (없으면 '개별 종목장')"
  }},
  "kimchi_premium": {{
    "status": "국내 수급 상태",
    "notable": "특이사항 (없으면 null)"
  }},
  "regulation": {{
    "us": "미국 규제 동향 (없으면 null)",
    "clarity_act": "클래리티 법안 진행 상황 (없으면 null)",
    "global": "기타 국가 규제 (없으면 null)"
  }},
  "overall_sentiment": "전체 시장 분위기 2~3줄 요약",
  "key_risk": "오늘 가장 큰 리스크 요인"
}}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={clean_key}")

    for idx in range(3):
        try:
            print(f"  🔄 [시도 {idx+1}/3] 내러티브 수집 중...")
            resp = requests.post(url, json=payload, timeout=120)
            if resp.status_code == 200:
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text  = next((p["text"] for p in parts if "text" in p), None)
                if not text:
                    continue
                cleaned = re.sub(r"```json|```", "", text).strip()
                f, l = cleaned.find("{"), cleaned.rfind("}")
                if f != -1 and l != -1:
                    parsed = json.loads(cleaned[f:l+1])
                    print("  ✅ 시장 내러티브 수집 완료")
                    return parsed
            else:
                wait = 30 if resp.status_code == 429 else 5
                print(f"  ⚠️ HTTP {resp.status_code} — {wait}초 대기")
                time.sleep(wait)
                continue
        except Exception as e:
            print(f"  ⚠️ [시도 {idx+1}] 에러: {e}")
        time.sleep(5)
    return {}


# ==========================================
# 4-B. 2호출 — 개별 코인 촉매 분석
# ==========================================
def fetch_coin_narratives(target_coins: list, top30_coins: list) -> dict:
    print("🔍 [2호출] 개별 코인 촉매 분석 중 (Gemini + 구글서치)...")
    clean_key = GEMINI_API_KEY.strip()
    if not clean_key:
        return {}

    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")

    # 분석 대상 코인 목록 (상위 15개만 — 토큰 절약)
    all_coins = top30_coins[:5] + target_coins[:10]
    coin_list = "\n".join(
        f"- {t}: {v['price']:,}원 / 변동 {v['change']:+.1f}% / 거래대금 {v['volume']/1e8:.0f}억"
        for t, v in all_coins
    )

    prompt = f"""
오늘({today_str}) 아래 코인들의 개별 촉매와 내러티브를 구글서치로 찾아서 분석해라.
코인니스(coinness.com) 기사를 우선으로 참고해라.
최근 7일 이내 뉴스만 사용해라.

[분석 대상 코인]
{coin_list}

각 코인에 대해 아래를 찾아라:
1. 오늘 움직임의 구체적 촉매 (이벤트/뉴스/업데이트)
2. 현재 내러티브가 초입인지 중반인지 과열인지
3. 언락 일정 (있으면)
4. 진입 시 주의사항

출력은 순수 JSON만 반환해라:
{{
  "coin_narratives": {{
    "티커": {{
      "catalyst": "구체적 촉매 (없으면 null)",
      "narrative_phase": "초입 또는 중반 또는 과열 또는 해당없음",
      "unlock_schedule": "언락 일정 (없으면 null)",
      "caution": "주의사항 (없으면 null)",
      "summary": "한 줄 요약"
    }}
  }}
}}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={clean_key}")

    for idx in range(3):
        try:
            print(f"  🔄 [시도 {idx+1}/3] 코인 촉매 분석 중...")
            resp = requests.post(url, json=payload, timeout=120)
            if resp.status_code == 200:
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text  = next((p["text"] for p in parts if "text" in p), None)
                if not text:
                    continue
                cleaned = re.sub(r"```json|```", "", text).strip()
                f, l = cleaned.find("{"), cleaned.rfind("}")
                if f != -1 and l != -1:
                    parsed = json.loads(cleaned[f:l+1])
                    # 티커 키 대문자 정규화
                    cn = parsed.get("coin_narratives", {})
                    if cn:
                        parsed["coin_narratives"] = {k.upper(): v for k, v in cn.items()}
                    print("  ✅ 개별 코인 촉매 분석 완료")
                    return parsed
            else:
                wait = 30 if resp.status_code == 429 else 5
                print(f"  ⚠️ HTTP {resp.status_code} — {wait}초 대기")
                time.sleep(wait)
                continue
        except Exception as e:
            print(f"  ⚠️ [시도 {idx+1}] 에러: {e}")
        time.sleep(5)
    return {}


# ==========================================
# 4-C. narrative.json GitHub 저장
# ==========================================
def save_narrative_to_github(narrative: dict, coin_narratives: dict, publish_time: str):
    print("💾 내러티브 데이터 저장 중 (GitHub)...")
    try:
        existing, sha = _gh_read("data/narrative.json")
        if not isinstance(existing, list):
            existing = []
        raw_cn = coin_narratives.get("coin_narratives", {}) if coin_narratives else {}
        existing.append({
            "발행일시": publish_time,
            "market_narrative": narrative,
            "coin_narratives": {k.upper(): v for k, v in raw_cn.items()},
        })
        # 최근 30개만 유지
        if len(existing) > 30:
            existing = existing[-30:]
        ok = _gh_write("data/narrative.json", existing, sha, f"내러티브: {publish_time}")
        print(f"  {'✅' if ok else '❌'} narrative.json 저장")
    except Exception as e:
        print(f"  ❌ 내러티브 저장 실패: {e}")


def _calc_pct(entry, target, abs_val=False) -> float:
    """진입가 대비 목표가/손절가 수익률 계산"""
    try:
        e = float(str(entry).replace(",", ""))
        t = float(str(target).replace(",", ""))
        if e <= 0: return 0.0
        pct = round((t - e) / e * 100, 1)
        return round(abs(pct), 1) if abs_val else pct
    except Exception:
        return 0.0


# ==========================================
# 4. Gemini AI 분석 (3호출 — 종목 선별)
# ==========================================
def generate_market_insights_via_gemini(
    target_coins: list,
    top30_coins: list,
    indicators: dict,
    top_30_cutoff: float,
    market_activity: dict,
    strengthened_rules: str = "",
    performance_summary: str = "",
    market_narrative: dict = None,
    coin_narratives: dict = None,
) -> Optional[dict]:
    print("🤖 Gemini AI 분석 중 (TOP30 2종 + 급등후보 4종 = 총 6종목)...")

    clean_key = GEMINI_API_KEY.strip()
    if not clean_key:
        return None

    ma = market_activity
    vs_str = f"전월 평균 대비 {ma['vs_avg_pct']}%" if ma.get("vs_avg_pct") else "비교 불가"
    activity_ctx = (
        f"전체 KRW 거래대금: {ma['total_volume']/1e12:.2f}조원 ({vs_str}) | {ma['level_label']}\n"
        f"수급 성격: {ma['supply_character']} | 메이저 {ma['major_ratio']}% / 알트 {ma['alt_ratio']}%\n"
    )

    # 31~80위 수급급증 목록
    market_list = ""
    for rank, (ticker, info) in enumerate(target_coins, start=31):
        wl_mark = " ⭐관심종목" if info.get("is_watchlist") else ""
        market_list += (
            f"- {rank}위 {ticker}{wl_mark}: {info['price']:,}원 / "
            f"거래대금 {info['volume']/1e8:.0f}억 / 변동 {info['change']:+.1f}%\n"
        )

    # TOP30 메이저 알트 목록 (스테이블 제외)
    top30_list = ""
    for rank, (ticker, info) in enumerate(top30_coins, start=1):
        top30_list += (
            f"- {rank}위 {ticker}: {info['price']:,}원 / "
            f"거래대금 {info['volume']/1e8:.0f}억 / 변동 {info['change']:+.1f}%\n"
        )

    rules_sec = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n▶ 강화 규칙 (반드시 준수)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + strengthened_rules + "\n\n"
    ) if strengthened_rules else ""

    perf_sec = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n▶ 직전 추천 성과\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + performance_summary + "\n\n"
    ) if performance_summary else ""

    system_prompt = (
        "너는 한국 코인 시장 전문 퀀트 애널리스트다.\n"
        "제공된 데이터를 바탕으로 아래 구조로 총 6종목을 정확하게 선별해라.\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "▶ 선별 구조\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "perspective 값 (반드시 아래 4가지 중 하나만 사용):\n"
        "  'TOP30 돌파'    — 거래대금 TOP30 대형 코인 중 지금 올라오는 것\n"
        "  'TOP30 눌림목'  — 거래대금 TOP30 대형 코인 중 저점에서 반등 노리는 것\n"
        "  '급등후보 돌파'  — 31~80위 중 수급이 갑자기 터지는 것\n"
        "  '급등후보 눌림목'— 31~80위 중 저점 매수 타이밍 노리는 것\n\n"

        "종목 수:\n"
        "  TOP30 돌파 1종 + TOP30 눌림목 1종 = 2종\n"
        "  급등후보 돌파 2종 + 급등후보 눌림목 2종 = 4종\n"
        "  합계 6종목\n\n"

        "[TOP30 돌파/눌림목] TOP30 풀(스테이블·BTC 제외)에서 선정:\n"
        "  · 돌파: 1h/6h 거래량 130%↑, 볼밴 중단 돌파 초입, BTC 비연동 또는 독자 수급\n"
        "  · 눌림목: 일봉 RSI 40~58, 장기 이평 지지 확인, BTC 대비 방어력 [중] 이상\n"
        "  · TOP30은 포지션 크기를 급등후보보다 1.5~2배 크게 권장\n\n"

        "[급등후보 돌파/눌림목] 31~80위 수급급증 풀에서 선정:\n"
        "  · 돌파: 1h/6h 거래량 150%↑, 볼밴 중단 돌파, 거래량 다이버전스 확인\n"
        "  · 눌림목: 일봉 RSI 38~55, EMA 수렴, 방어력 [상] 이상\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "▶ 진입가 계산 규칙 (절대 '시장가' 금지)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "[돌파] entry = 현재가 × 1.005 이내 숫자로 명시\n"
        "[눌림목] entry = EMA20 또는 볼밴 중단 기준 계산한 숫자\n"
        "T1 = entry × 1.08 ~ 1.12 (1차 50% 익절)\n"
        "T2 = entry × 1.15 ~ 1.25 (2차 홀딩)\n"
        "stop_loss = 직전 저점 또는 EMA60 이탈가\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "▶ 시장 온도별 보수성 조절\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧊 냉각: 손절 타이트 (-3~4%), 급등후보 돌파 1개만, TOP30 위주 진입\n"
        "😴 관망: 손절 -4~5%, 분할 진입 강조\n"
        "😊 보통: 기준대로\n"
        "🔥 활성화: 손절 -5~7%, T2 높게 설정\n"
        "🚀 과열: 급등후보 돌파 자제, TOP30 눌림목 위주\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "▶ why_down / why_still (구글 검색 필수)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "why_down: 구글 검색으로 최근 7일 내 하락 원인\n"
        "why_still: 그 악재가 이미 반영됐거나 일시적임을 보여주는 근거\n\n"

        + rules_sec + perf_sec

        + "출력은 순수 JSON만 반환해라.\n\n"

        '반환 JSON 스키마:\n'
        '{\n'
        '  "market_mood": "시장 분위기 한 줄 요약",\n'
        '  "editor_pick_ticker": "6종목 중 오늘 가장 추천하는 단 1개의 티커",\n'
        '  "editor_pick_reason": "편집장 픽 이유 1~2문장 (초보자도 이해할 수 있게 쉽게)",\n'
        '  "picks": [\n'
        '    {\n'
        '      "perspective": "TOP30 돌파 또는 TOP30 눌림목 또는 급등후보 돌파 또는 급등후보 눌림목",\n'
        '      "rank_in_perspective": 1,\n'
        '      "category": "테마명 (영어약어 없이 한글로)",\n'
        '      "ticker": "티커",\n'
        '      "ticker_full_name": "코인 정식 이름",\n'
        '      "ticker_description": "초보자용 한 줄 설명",\n'
        '      "current_price_krw": "현재가",\n'
        '      "change_24h": "+0.00%",\n'
        '      "change_num": 0.0,\n'
        '      "unlock_alert": "언락 경보 또는 null",\n'
        '      "kimp": "김프 수치",\n'
        '      "kimp_action": "김프 진입 유불리 해석",\n'
        '      "btc_sync": "BTC 동조화",\n'
        '      "defense": "방어력",\n'
        '      "vol_divergence": "거래량 다이버전스",\n'
        '      "trend_reverse": "추세 역행 분석",\n'
        '      "rank_change": "순위 변화 또는 null",\n'
        '      "entry": "원화 숫자",\n'
        '      "entry_logic": "진입 근거 (쉬운 말로)",\n'
        '      "t1": "1차 목표가",\n'
        '      "t1_pct": 0.0,\n'
        '      "t2": "2차 목표가",\n'
        '      "t2_pct": 0.0,\n'
        '      "stop_loss": "손절가",\n'
        '      "stop_loss_pct": 0.0,\n'
        '      "position_size": "권장 비중",\n'
        '      "holding_period": "기간",\n'
        '      "why_down": "하락 이유",\n'
        '      "why_still": "추천 이유",\n'
        '      "tech_signal": "기술적 근거",\n'
        '      "whale_signal": "수급 동향",\n'
        '      "what_if_t1_miss": "1차 목표 미달성 시 대응",\n'
        '      "what_if_btc_drop": "BTC 급락 시 대응",\n'
        '      "score": "⭐⭐⭐⭐☆",\n'
        '      "related_coins": ["티커"],\n'
        '      "source": "출처",\n'
        '      "coin_narrative": "개별 코인 촉매/내러티브 단계/주의사항 요약"\n'
        '    }\n'
        '  ],\n'
        '  "keywords": ["키워드1"]\n'
        '}'
    )

    system_prompt += (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "▶ 필드 작성 지침\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ticker_description: 코린이도 바로 이해할 수 있는 수준으로.\n"
        "unlock_alert: 구글 검색으로 최근 30일 내 언락 일정 확인. 없으면 null.\n"
        "kimp_action: 5%↑이면 '고김프 — 과매수 주의', -1%↓이면 '역김프 — 진입 유리' 형식.\n"
        "position_size: TOP30은 급등후보보다 1.5~2배 크게. 시장온도도 반영.\n"
        "what_if_t1_miss / what_if_btc_drop: 종목별로 반드시 다르게 작성.\n"
        "category: 영어 약어 없이 한글만. RWA→실물자산 토큰화, DePIN→분산형 인프라.\n"
    )

    # ── 내러티브 컨텍스트 구성 ──
    narrative_ctx = ""
    if market_narrative:
        mn = market_narrative
        narrative_ctx += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        narrative_ctx += "▶ 오늘 시장 내러티브 (코인니스 + 구글서치 기반)\n"
        narrative_ctx += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        narrative_ctx += f"[전체 분위기] {mn.get('overall_sentiment','—')}\n"
        narrative_ctx += f"[핵심 리스크] {mn.get('key_risk','—')}\n\n"

        macro = mn.get("macro", {})
        if macro:
            narrative_ctx += f"[거시경제]\n"
            if macro.get("fed"):    narrative_ctx += f"  • 연준: {macro['fed']}\n"
            if macro.get("nasdaq"): narrative_ctx += f"  • 나스닥: {macro['nasdaq']}\n"
            if macro.get("dxy"):    narrative_ctx += f"  • 달러: {macro['dxy']}\n"
            narrative_ctx += f"  → {macro.get('summary','')}\n\n"

        btc = mn.get("btc", {})
        if btc:
            narrative_ctx += f"[BTC 특이사항]\n"
            if btc.get("etf_flow"):     narrative_ctx += f"  • ETF: {btc['etf_flow']}\n"
            if btc.get("whale"):        narrative_ctx += f"  • 고래: {btc['whale']}\n"
            if btc.get("liquidation"):  narrative_ctx += f"  • 청산: {btc['liquidation']}\n"
            if btc.get("institution"):  narrative_ctx += f"  • 기관: {btc['institution']}\n"
            narrative_ctx += f"  → {btc.get('summary','')}\n\n"

        sectors = mn.get("sectors", {})
        if sectors:
            narrative_ctx += f"[섹터 흐름] 주도 테마: {sectors.get('dominant_theme','—')}\n"
            hot = sectors.get("hot", [])
            if hot:
                narrative_ctx += "  🔥 강한 섹터: " + ", ".join(
                    f"{s['name']}({s.get('reason','')})" for s in hot[:3]) + "\n"
            cold = sectors.get("cold", [])
            if cold:
                narrative_ctx += "  ❄️ 약한 섹터: " + ", ".join(
                    f"{s['name']}({s.get('reason','')})" for s in cold[:3]) + "\n"
            narrative_ctx += "\n"

        reg = mn.get("regulation", {})
        if reg and any(reg.values()):
            narrative_ctx += "[규제/정책]\n"
            if reg.get("clarity_act"): narrative_ctx += f"  • 클래리티: {reg['clarity_act']}\n"
            if reg.get("us"):          narrative_ctx += f"  • 미국: {reg['us']}\n"
            if reg.get("global"):      narrative_ctx += f"  • 글로벌: {reg['global']}\n"
            narrative_ctx += "\n"

    coin_narrative_ctx = ""
    if coin_narratives:
        cn = coin_narratives.get("coin_narratives", {})
        if cn:
            coin_narrative_ctx = "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            coin_narrative_ctx += "▶ 개별 코인 촉매 분석\n"
            coin_narrative_ctx += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for ticker, info in cn.items():
                coin_narrative_ctx += f"[{ticker}]\n"
                coin_narrative_ctx += f"  촉매: {info.get('catalyst','없음')}\n"
                coin_narrative_ctx += f"  내러티브 단계: {info.get('narrative_phase','—')}\n"
                if info.get("unlock_schedule"):
                    coin_narrative_ctx += f"  언락: {info['unlock_schedule']}\n"
                if info.get("caution"):
                    coin_narrative_ctx += f"  주의: {info['caution']}\n"
                coin_narrative_ctx += "\n"

    user_query = f"""
[시장 현황]
{activity_ctx}
[TOP30 컷오프: {top_30_cutoff:,.0f}원]

[TOP30 대형 알트 (스테이블·BTC 제외) — TOP30 돌파/눌림목 후보]
{top30_list}

[빗썸 31~80위 수급 급증 상위 30개 — 급등후보 돌파/눌림목 후보]
{market_list}

[바이낸스 멀티타임프레임 + BTC 동조화 + 김프 지표]
{format_indicators_for_prompt(indicators, target_coins + top30_coins)}

{narrative_ctx}
{coin_narrative_ctx}

★ 지시사항 ★
1. TOP30 돌파 1종 + TOP30 눌림목 1종 + 급등후보 돌파 2종 + 급등후보 눌림목 2종 = 총 6종목.
2. TOP30 종목은 반드시 TOP30 풀(위 목록)에서만 선택.
3. 급등후보 종목은 31~80위 풀에서만 선택.
4. 진입가·T1·T2·손절가는 반드시 원화 숫자로 명시. '시장가' 절대 금지.
5. what_if_t1_miss와 what_if_btc_drop은 종목별로 각각 다르게 작성.
6. 시장 온도 '{ma['level_label']}'에 맞게 손절/목표·position_size 조정.
7. unlock_alert는 개별 코인 촉매 분석 데이터 참고 후 작성.
8. ticker_description은 코인을 전혀 모르는 사람도 이해할 수 있게 작성.
9. editor_pick_ticker는 6종목 중 오늘 시장 온도와 가장 잘 맞는 1종목만 선택.
10. 순수 JSON만 반환.
11. 위 시장 내러티브와 개별 코인 촉매를 반드시 종목 선별에 반영해라.
    - 거시 악재가 크면 TOP30 눌림목 위주 + 급등후보 돌파 자제
    - 섹터 흐름이 살아있는 코인 우선 선별
    - 내러티브 단계가 '과열'인 코인은 추격 경고 표시
    - 촉매 없는 급등 코인은 coin_narrative에 '촉매 미확인 — 추격 주의' 명시
12. coin_narrative 필드에 개별 코인 촉매 분석 내용을 간결하게 작성.
13. ⭐관심종목 표시된 종목은 반드시 분석에 포함하고 가능하면 추천 목록에 넣어라.
    단, 기술적/수급 조건이 매우 불리하면 제외하되 이유를 market_mood에 명시해라.
"""

    # google_search 툴 사용 시 responseMimeType 제거 (충돌 방지)
    payload = {
        "contents":          [{"parts": [{"text": user_query}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "tools":             [{"google_search": {}}],
        "generationConfig":  {"temperature": 0.2},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={clean_key}")

    for idx in range(3):
        try:
            print(f"🔄 [시도 {idx+1}/3] Gemini 호출 중...")
            resp = requests.post(url, json=payload, timeout=150)
            if resp.status_code == 200:
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text  = next((p["text"] for p in parts if "text" in p), None)
                if not text: continue
                cleaned = re.sub(r"```json|```", "", text).strip()
                f, l = cleaned.find("{"), cleaned.rfind("}")
                if f != -1 and l != -1:
                    parsed = json.loads(cleaned[f:l+1])
                    print("✅ Gemini 분석 완료")
                    return parsed
            else:
                print(f"  ⚠️ Gemini HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"⚠️ [시도 {idx+1}] 에러: {e}")
        time.sleep(3)
    return None


# ==========================================
# GitHub JSON DB — 구글 시트 대체
# ==========================================
# 데이터 구조:
#   data/picks.json       — 추천기록 누적
#   data/performance.json — 성과추적 누적
#   data/review.json      — 복기분석 누적
#   data/rules.json       — 강화규칙 누적

import base64

GITHUB_REPO_ID = "insight-doby/report_coin"


def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN.strip()}",
        "Accept": "application/vnd.github.v3+json",
    }


def _gh_read(path: str) -> tuple:
    """깃허브에서 JSON 파일 읽기. (data, sha) 반환. 없으면 ([], None)"""
    url = f"https://api.github.com/repos/{GITHUB_REPO_ID}/contents/{path}"
    r   = requests.get(url, headers=_gh_headers(), timeout=10)
    if r.status_code == 200:
        info    = r.json()
        content = base64.b64decode(info["content"]).decode("utf-8")
        return json.loads(content), info["sha"]
    return [], None


def _gh_write(path: str, data, sha: str = None, msg: str = "데이터 업데이트"):
    """깃허브에 JSON 파일 쓰기 (없으면 생성, 있으면 덮어쓰기)"""
    url     = f"https://api.github.com/repos/{GITHUB_REPO_ID}/contents/{path}"
    content = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()
    body    = {"message": msg, "content": content}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
    return r.status_code in (200, 201)


# ==========================================
# [1단계] 추천 기록 저장
# ==========================================
def record_picks_to_github(picks: list, market_activity: dict, publish_time: str):
    print("📝 [1단계] 추천 기록 저장 중 (GitHub)...")
    try:
        existing, sha = _gh_read("data/picks.json")
        if not isinstance(existing, list):
            existing = []

        for p in picks:
            existing.append({
                "추천일시":     publish_time,
                "티커":        p.get("ticker", ""),
                "관점":        p.get("perspective", ""),
                "진입가":      p.get("entry", ""),
                "T1":          p.get("t1", ""),
                "T2":          p.get("t2", ""),
                "손절가":      p.get("stop_loss", ""),
                "T1수익률":    _calc_pct(p.get("entry"), p.get("t1")),
                "T2수익률":    _calc_pct(p.get("entry"), p.get("t2")),
                "손절률":      _calc_pct(p.get("entry"), p.get("stop_loss"), abs_val=True),
                "시장온도":    market_activity.get("level_label", ""),
                "메이저비중":  market_activity.get("major_ratio", ""),
                "알트비중":    market_activity.get("alt_ratio", ""),
                "수급성격":    market_activity.get("supply_character", ""),
                "총거래대금":  f"{market_activity.get('total_volume', 0)/1e12:.2f}조원",
                "김프":        p.get("kimp", ""),
                "BTC동조화":   p.get("btc_sync", ""),
                "방어력":      p.get("defense", ""),
                "거래량다이버전스": p.get("vol_divergence", ""),
                "추세역행":    p.get("trend_reverse", ""),
                "why_down":    p.get("why_down", ""),
                "why_still":   p.get("why_still", ""),
                "what_if":     p.get("what_if_t1_miss", "") or p.get("what_if", ""),
                "신뢰도":      p.get("score", ""),
                "현재가":      p.get("current_price_krw", ""),
                "coin_narrative": p.get("coin_narrative", ""),
                "기록상태":    "신규",
            })

        ok = _gh_write("data/picks.json", existing, sha, f"추천기록: {publish_time}")
        print(f"  {'✅' if ok else '❌'} {len(picks)}개 저장 완료")
    except Exception as e:
        print(f"  ❌ 저장 실패: {e}")


# ==========================================
# [2단계] 성과 추적
# ==========================================
def parse_price(s: str) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.]", "", str(s))) or None
    except:
        return None


def fetch_current_price_bithumb(ticker: str) -> Optional[float]:
    try:
        r = requests.get(f"https://api.bithumb.com/public/ticker/{ticker}_KRW", timeout=5)
        if r.status_code == 200:
            d = r.json()
            if d.get("status") == "0000":
                return float(d["data"]["closing_price"])
    except:
        pass
    return None


def track_performance() -> list:
    print("📊 [2단계] 성과 추적 중 (GitHub)...")
    results = []
    try:
        picks_data, picks_sha = _gh_read("data/picks.json")
        perf_data,  perf_sha  = _gh_read("data/performance.json")
        if not isinstance(picks_data, list): picks_data = []
        if not isinstance(perf_data,  list): perf_data  = []

        now_str  = datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M")
        updated  = False

        for pick in picks_data:
            if pick.get("기록상태") != "신규":
                continue

            ticker = str(pick.get("티커", "")).upper()
            entry  = parse_price(pick.get("진입가"))
            t1     = parse_price(pick.get("T1"))
            t2     = parse_price(pick.get("T2"))
            sl     = parse_price(pick.get("손절가"))
            if not entry or entry == 0:
                continue

            cur = fetch_current_price_bithumb(ticker)
            if not cur:
                continue

            pnl    = round((cur - entry) / entry * 100, 2)
            result = "진행중"
            if   t2 and cur >= t2: result = "✅ T2 달성"
            elif t1 and cur >= t1: result = "📈 T1 달성"
            elif sl and cur <= sl: result = "❌ 손절"
            elif pnl >= 5:         result = "📈 수익 중"
            elif pnl <= -3:        result = "⚠️ 손실 중"

            perf_data.append({
                "추천일시":  pick.get("추천일시", ""),
                "티커":     ticker,
                "관점":     pick.get("관점", ""),
                "진입가":   pick.get("진입가", ""),
                "T1":       pick.get("T1", ""),
                "T2":       pick.get("T2", ""),
                "손절가":   pick.get("손절가", ""),
                "확인일시": now_str,
                "확인가격": str(int(cur)),
                "수익률":   f"{pnl:+.2f}%",
                "결과":     result,
                "T1달성":   "Y" if t1 and cur >= t1 else "N",
                "T2달성":   "Y" if t2 and cur >= t2 else "N",
                "손절":     "Y" if sl and cur <= sl else "N",
                "시장온도": pick.get("시장온도", ""),
            })

            pick["기록상태"] = "완료" if "달성" in result or "손절" in result else "추적중"
            updated = True

            results.append({"ticker": ticker, "entry": entry, "current": cur,
                            "pnl_pct": pnl, "result": result})
            print(f"  {'✅' if '달성' in result else '❌' if '손절' in result else '📊'} "
                  f"{ticker}: {entry:,.0f}→{cur:,.0f} ({pnl:+.2f}%) [{result}]")
            time.sleep(0.1)

        if updated:
            _gh_write("data/picks.json",       picks_data, picks_sha, "성과추적: 상태 업데이트")
            _gh_write("data/performance.json", perf_data,  perf_sha,  f"성과추적: {now_str}")
            print(f"  ✅ 성과 {len(results)}건 저장 완료")
        else:
            print("  ℹ️ 신규 추적 항목 없음")

    except Exception as e:
        print(f"  ❌ 성과 추적 오류: {e}")
    return results


def performance_summary_text(results: list) -> str:
    if not results: return ""
    wins = [r for r in results if "달성" in r["result"]]
    sl   = [r for r in results if "손절" in r["result"]]
    wr   = round(len(wins)/len(results)*100, 1)
    avg  = round(sum(r["pnl_pct"] for r in results)/len(results), 2)
    lines = [f"총 {len(results)}건 | 승률 {wr}% | 평균수익 {avg:+.2f}%",
             f"✅ T달성 {len(wins)}건  ❌ 손절 {len(sl)}건  📊 진행중 {len(results)-len(wins)-len(sl)}건", ""]
    for r in results:
        icon = "✅" if "달성" in r["result"] else "❌" if "손절" in r["result"] else "📊"
        lines.append(f"{icon} {r['ticker']}: {r['entry']:,.0f}→{r['current']:,.0f} ({r['pnl_pct']:+.2f}%)")
    return "\n".join(lines)


# ==========================================
# [3단계] ML 복기
# ==========================================
def run_ml_review(results: list) -> dict:
    """
    GitHub performance.json 전체 데이터 기반으로 복기.
    당회 결과 없어도 누적 데이터 있으면 실행.
    """
    print("🔬 [3단계] ML 복기 분석 중 (GitHub)...")
    review = {}

    # 누적 성과 데이터 읽기
    all_perf_rows, _ = _gh_read("data/performance.json")
    if not isinstance(all_perf_rows, list): all_perf_rows = []
    print(f"  📂 누적 성과 데이터 {len(all_perf_rows)}건 로드")

    if not results and not all_perf_rows:
        print("  ℹ️ 분석할 데이터 없음 — 복기 스킵")
        return {}

    # Gemini 복기 분석
    try:
        cur_summary = performance_summary_text(results) if results else "당회 신규 성과 없음"
        total  = len(all_perf_rows)
        wins   = sum(1 for r in all_perf_rows if "달성" in str(r.get("결과","")))
        losses = sum(1 for r in all_perf_rows if "손절" in str(r.get("결과","")))
        accum_summary = (
            f"누적 총 {total}건 | 승 {wins}건 | 패 {losses}건 | "
            f"승률 {round(wins/total*100,1) if total else 0}%"
        )

        prompt = f"""
아래 코인 추천 성과를 분석하고 JSON으로 반환해라.

[당회 성과]
{cur_summary}

[누적 성과 요약]
{accum_summary}

[누적 상세 (최근 20건)]
{json.dumps(all_perf_rows[-20:], ensure_ascii=False)}

분석 지시:
1. 수익 종목들의 공통 패턴 (관점, 시장온도, 지표 특징)
2. 손절 종목들의 공통 패턴
3. 앞으로 추천 시 반드시 지켜야 할 규칙 2~3개
4. 절대 하지 말아야 할 패턴 1~2개

반환 JSON (JSON만):
{{
  "win_pattern": "수익 공통 패턴",
  "lose_pattern": "손실 공통 패턴",
  "market_insight": "시장 환경이 결과에 미친 영향",
  "rule_improvements": [{{"관점":"","개선규칙":"","근거":""}}],
  "never_do": "절대 하지 말아야 할 패턴",
  "next_focus": "다음 추천 시 핵심 포인트"
}}"""

        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY.strip()}")
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1}
        }, timeout=60)

        if r.status_code == 200:
            parts = r.json()["candidates"][0]["content"]["parts"]
            text  = re.sub(r"```json|```", "", next(
                (p["text"] for p in parts if "text" in p), ""
            )).strip()
            f_idx, l_idx = text.find("{"), text.rfind("}")
            if f_idx != -1:
                review = json.loads(text[f_idx:l_idx+1])
                print("  ✅ Gemini 복기 완료")
        else:
            print(f"  ⚠️ Gemini 복기 HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ 복기 실패: {e}")

    # ML 의사결정 트리
    try:
        from sklearn.tree import DecisionTreeClassifier, export_text
        import pandas as pd

        if len(all_perf_rows) >= 5:
            df = pd.DataFrame(all_perf_rows)
            df["pnl_val"] = df["수익률"].apply(
                lambda x: float(str(x).replace("%","").replace("+","")) if x else 0
            )
            df["y"] = (df["pnl_val"] >= 5).astype(int)
            temp_map = {"🧊 냉각":0,"😴 관망":1,"😊 보통":2,"🔥 활성화":3,"🚀 과열/광풍":4}
            df["temp_n"]   = df.get("시장온도", pd.Series()).map(temp_map).fillna(2)
            df["is_break"] = (df.get("관점", pd.Series()) == "급등후보 돌파").astype(int)
            features = ["temp_n", "is_break"]
            clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=2, random_state=42)
            clf.fit(df[features].values, df["y"].values)
            review["ml_tree"]     = export_text(clf, feature_names=features)
            review["ml_accuracy"] = round(clf.score(df[features].values, df["y"].values)*100, 1)
            print(f"  ✅ 의사결정트리 정확도 {review['ml_accuracy']}%")
        else:
            print(f"  ℹ️ ML 데이터 부족 ({len(all_perf_rows)}건 / 최소 5건)")
    except ImportError:
        print("  ℹ️ sklearn 없음")
    except Exception as e:
        print(f"  ⚠️ ML 오류: {e}")

    # 복기 결과 저장
    try:
        existing, sha = _gh_read("data/review.json")
        if not isinstance(existing, list): existing = []
        existing.append({
            "분석일시":   datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M"),
            "win_pattern":    review.get("win_pattern", ""),
            "lose_pattern":   review.get("lose_pattern", ""),
            "never_do":       review.get("never_do", ""),
            "market_insight": review.get("market_insight", ""),
            "next_focus":     review.get("next_focus", ""),
            "ml_accuracy":    review.get("ml_accuracy", ""),
        })
        _gh_write("data/review.json", existing, sha, "복기분석 저장")
        print("  ✅ 복기분석 저장 완료")
    except Exception as e:
        print(f"  ⚠️ 복기 저장 실패: {e}")

    return review


# ==========================================
# [4단계] 강화 규칙 추출
# ==========================================
def update_rules(review: dict) -> str:
    if not review: return ""
    print("🔧 [4단계] 강화 규칙 업데이트 (GitHub)...")
    try:
        existing, sha = _gh_read("data/rules.json")
        if not isinstance(existing, list): existing = []

        now = datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M")
        new_rules = review.get("rule_improvements", [])
        if not new_rules:
            active = [r for r in existing if r.get("적용여부") == "적용중"][-5:]
            return "\n".join(f"  • [{r.get('관점','')}] {r.get('규칙내용','')}" for r in active)

        # 기존 규칙 + 신규 규칙을 Gemini에게 정리 요청
        clean_key = GEMINI_API_KEY.strip()
        existing_text = "\n".join(
            f"- [{r.get('관점','')}] {r.get('규칙내용','')} (등록: {r.get('업데이트일시','')}, 상태: {r.get('적용여부','')})"
            for r in existing
        ) or "없음"
        new_text = "\n".join(
            f"- [{r.get('관점','')}] {r.get('개선규칙','')} (근거: {r.get('근거','')})"
            for r in new_rules
        )

        prompt = f"""
아래는 현재 적용 중인 강화규칙 목록과 새로 도출된 규칙이다.
두 목록을 합쳐서 최종 규칙 리스트를 정리해라.

[기존 규칙]
{existing_text}

[새로 도출된 규칙]
{new_text}

정리 기준:
1. 모순/상충하는 규칙 → 더 최근에 도출된 규칙 우선, 오래된 것 폐기
2. 유사한 규칙 → 하나로 합쳐서 더 구체적으로 작성
3. 6개월 이상 지난 규칙 → 폐기 검토
4. 최종 규칙은 최대 10개 이하로 유지
5. 폐기된 규칙도 기록에 남기되 상태를 "폐기"로 표시

순수 JSON만 반환해라:
{{
  "final_rules": [
    {{
      "관점": "TOP30 돌파 또는 TOP30 눌림목 또는 급등후보 돌파 또는 급등후보 눌림목 또는 공통",
      "규칙내용": "구체적인 규칙",
      "근거": "이 규칙을 유지/생성한 이유",
      "적용여부": "적용중 또는 폐기",
      "업데이트일시": "{now}"
    }}
  ],
  "changelog": "이번 업데이트에서 변경된 내용 한 줄 요약"
}}
"""
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1},
        }
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash:generateContent?key={clean_key}")

        for idx in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=60)
                if resp.status_code == 200:
                    parts = resp.json()["candidates"][0]["content"]["parts"]
                    text  = next((p["text"] for p in parts if "text" in p), None)
                    if not text: continue
                    cleaned = re.sub(r"```json|```", "", text).strip()
                    f, l = cleaned.find("{"), cleaned.rfind("}")
                    if f != -1 and l != -1:
                        parsed = json.loads(cleaned[f:l+1])
                        final_rules = parsed.get("final_rules", [])
                        changelog   = parsed.get("changelog", "")

                        # changelog 히스토리 보존
                        history = [r for r in existing if r.get("_type") == "changelog"]
                        if changelog:
                            history.append({"_type": "changelog", "일시": now, "내용": changelog})

                        # 최종 규칙 + 히스토리 저장
                        _gh_write("data/rules.json", final_rules + history, sha, f"강화규칙 정리: {now}")

                        active = [r for r in final_rules if r.get("적용여부") == "적용중"]
                        rules_text = "\n".join(
                            f"  • [{r.get('관점','')}] {r.get('규칙내용','')}" for r in active
                        )
                        print(f"  ✅ {len(active)}개 규칙 적용 중 (폐기: {len(final_rules)-len(active)}개)")
                        return rules_text
                else:
                    wait = 30 if resp.status_code == 429 else 5
                    time.sleep(wait)
                    continue
            except Exception as e:
                print(f"  ⚠️ [시도 {idx+1}] 에러: {e}")
            time.sleep(5)

        # Gemini 실패 시 기존 방식으로 폴백
        print("  ⚠️ Gemini 규칙 정리 실패 — 기존 방식으로 저장")
        for item in new_rules:
            existing.append({
                "업데이트일시": now,
                "관점":        item.get("관점", ""),
                "규칙내용":    item.get("개선규칙", ""),
                "근거":        item.get("근거", ""),
                "적용여부":    "적용중",
            })
        if len(existing) > 30:
            existing = existing[-30:]
        _gh_write("data/rules.json", existing, sha, f"강화규칙 업데이트(폴백): {now}")
        active = [r for r in existing if r.get("적용여부") == "적용중"][-5:]
        return "\n".join(f"  • [{r.get('관점','')}] {r.get('규칙내용','')}" for r in active)

    except Exception as e:
        print(f"  ⚠️ 규칙 업데이트 실패: {e}")
        return ""


# ==========================================
# 5. Slack Block Kit 빌더 (v2 — 초보자 친화형)
# ==========================================

def _rr_bar(t2_pct: float, sl_pct: float) -> str:
    """손익비 텍스트 막대 생성"""
    rr = round(t2_pct / sl_pct, 1) if sl_pct > 0 else 0
    win_bar  = "█" * min(int(t2_pct / 2), 15)
    lose_bar = "█" * min(int(sl_pct / 2), 10)
    verdict  = "✅ 유리한 거래" if rr >= 2.0 else "⚠️ 보통 거래" if rr >= 1.5 else "❌ 불리한 거래"
    return (
        f"벌 수 있음   {win_bar} 최대 +{t2_pct:.1f}%\n"
        f"잃을 수 있음 {lose_bar} 최대 -{sl_pct:.1f}%\n"
        f"손익비 {rr}:1 — {verdict} (2:1 이상이면 진입 고려 가능)"
    )


def build_slack_blocks(
    data: dict, publish_time: str,
    market_activity: dict, performance_summary: str = ""
) -> list:

    PERSP_EMOJI = {
        "TOP30 돌파":    "🏆",
        "TOP30 눌림목":  "🏛️",
        "급등후보 돌파":  "🚀",
        "급등후보 눌림목":"📉",
    }
    PERSP_COLOR = {
        "TOP30 돌파":    "🟪",
        "TOP30 눌림목":  "🟦",
        "급등후보 돌파":  "🟩",
        "급등후보 눌림목":"🟧",
    }
    CARD_NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]

    ma  = market_activity
    pct = ma.get("vs_avg_pct", 0) or 0

    if   pct >= 200: strategy = "극도 과열입니다. 신규 진입 자제, 보유 중이라면 익절을 검토하세요."
    elif pct >= 130: strategy = "수급이 들어오고 있습니다. 추세 추종 유효, 모멘텀 코인 대응 가능합니다."
    elif pct >= 70:  strategy = "평상시 수준입니다. 분할 매수로 리스크를 분산하세요."
    elif pct >= 40:  strategy = "거래가 한산합니다. 트리거 신호가 날 때까지 대기를 권장합니다."
    else:            strategy = "극도로 냉각된 시장입니다. 신규 진입 자제, 손절 기준을 타이트하게 잡으세요."

    vs_str = f"전월 평균 대비 *{ma['vs_avg_pct']}%*" if ma.get("vs_avg_pct") else "기준치 산출 중"
    maj5   = "  ".join(f"`{t}` {v/1e8:.0f}억 {'+' if c>=0 else ''}{c:.1f}%"
                       for t, v, c in ma.get("major_top5", [])[:4])
    alt5   = "  ".join(f"`{t}` {v/1e8:.0f}억 {'+' if c>=0 else ''}{c:.1f}%"
                       for t, v, c in ma.get("alt_top5", [])[:4])

    editor_ticker = data.get("editor_pick_ticker", "")
    editor_reason = data.get("editor_pick_reason", "")

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": "🚀 차기 주도주 리포트 (TOP30 2종 + 급등후보 4종 = 총 6종목)", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"🕐 {publish_time}  |  빗썸 31~80위 스캔  |  Gemini AI + BTC 동조화 + 김프"}]},
        {"type": "divider"},
    ]

    # 시장 온도계
    blocks += [
        {"type": "header", "text": {"type": "plain_text", "text": "🌡️ 시장 온도계", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*전체 거래대금 (오늘)*\n{ma['total_volume']/1e12:.2f}조원\n({vs_str})"},
            {"type": "mrkdwn", "text": f"*시장 온도*\n{ma['level_label']}"},
            {"type": "mrkdwn", "text": f"*수급 주도*\n{ma['supply_character']}"},
            {"type": "mrkdwn", "text": f"*메이저코인 / 알트코인 비중*\n{ma['major_ratio']}% / {ma['alt_ratio']}%"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"🔵 *메이저 TOP 4* (시총 상위 대형 코인)\n{maj5}\n\n"
            f"🟠 *알트 TOP 4* (중소형 코인)\n{alt5}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"📌 *오늘의 전략*\n{strategy}"}},
        {"type": "divider"},
    ]

    # 편집장 픽
    if editor_ticker and editor_reason:
        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"⭐ *편집장 픽 — 오늘 1종목만 골라야 한다면?*\n"
                f"*`{editor_ticker}`* — {editor_reason}"}},
            {"type": "divider"},
        ]

    # 직전 성과
    if performance_summary:
        blocks += [
            {"type": "header", "text": {"type": "plain_text", "text": "📊 직전 추천 성과", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{performance_summary}\n```"}},
            {"type": "divider"},
        ]

    # 시장 분위기
    mood = data.get("market_mood", "")
    if mood:
        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"💬 *시장 분위기 요약*\n{mood}"}},
            {"type": "divider"},
        ]

    # 시장 내러티브 (1호출 결과)
    mn = data.get("market_narrative_data", {})
    if mn:
        sentiment = mn.get("overall_sentiment", "")
        key_risk  = mn.get("key_risk", "")
        sectors   = mn.get("sectors", {})
        dominant  = sectors.get("dominant_theme", "")
        hot_list  = "  ".join(f"`{s['name']}`" for s in sectors.get("hot", [])[:3])
        cold_list = "  ".join(f"`{s['name']}`" for s in sectors.get("cold", [])[:3])

        narrative_text = f"📰 *오늘의 시장 내러티브*\n{sentiment}\n"
        if dominant:  narrative_text += f"\n🔥 *주도 테마:* {dominant}"
        if hot_list:  narrative_text += f"\n✅ 강한 섹터: {hot_list}"
        if cold_list: narrative_text += f"\n❌ 약한 섹터: {cold_list}"
        if key_risk:  narrative_text += f"\n⚠️ *핵심 리스크:* {key_risk}"

        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": narrative_text}},
            {"type": "divider"},
        ]

    blocks.append({"type": "header", "text": {"type": "plain_text",
        "text": "🎯 종목 분석 (TOP30 2종 + 급등후보 4종)", "emoji": True}})

    picks = data.get("picks", [])
    for i, p in enumerate(picks[:6]):
        persp      = p.get("perspective", "")
        pe         = PERSP_EMOJI.get(persp, "📌")
        pc         = PERSP_COLOR.get(persp, "🟨")
        change_num = float(p.get("change_num", 0) or 0)
        arrow      = "🟢" if change_num >= 0 else "🔴"
        t1_pct     = float(p.get("t1_pct") or 0)
        t2_pct     = float(p.get("t2_pct") or 0)
        sl_pct     = float(p.get("stop_loss_pct") or 0)

        # 가격 기반으로 직접 재계산 (Gemini가 0으로 채울 경우 대비)
        try:
            entry_price = float(str(p.get("entry", 0)).replace(",", ""))
            t1_price    = float(str(p.get("t1", 0)).replace(",", ""))
            t2_price    = float(str(p.get("t2", 0)).replace(",", ""))
            sl_price    = float(str(p.get("stop_loss", 0)).replace(",", ""))
            if entry_price > 0:
                if t1_price > 0:  t1_pct = round((t1_price - entry_price) / entry_price * 100, 1)
                if t2_price > 0:  t2_pct = round((t2_price - entry_price) / entry_price * 100, 1)
                if sl_price > 0:  sl_pct = round(abs((sl_price - entry_price) / entry_price * 100), 1)
        except Exception:
            pass
        is_editor  = (p.get("ticker", "") == editor_ticker)
        is_watchlist = p.get("ticker", "") in WATCHLIST

        ticker       = p.get("ticker", "")
        full_name    = p.get("ticker_full_name", "")
        description  = p.get("ticker_description", "")
        category     = p.get("category", "")
        unlock_alert = p.get("unlock_alert") or ""
        rank_change  = p.get("rank_change") or ""
        position_sz  = p.get("position_size", "—")
        kimp_action  = p.get("kimp_action", "—")
        what_if_t1   = p.get("what_if_t1_miss") or p.get("what_if", "—")
        what_if_btc  = p.get("what_if_btc_drop") or "BTC 급락 시 손절가 기준으로 대응"
        rr_text      = _rr_bar(t2_pct, sl_pct)
        editor_mark  = "  ⭐ 편집장 픽" if is_editor else ""
        watchlist_mark = "  📌 관심종목" if is_watchlist else ""

        # ── 블록 1: 헤더 + 지표 + 매매셋업 (하나로 합침) ──
        unlock_line  = f"\n🚨 *이벤트 경보* {unlock_alert}" if unlock_alert else ""
        rank_line    = f"\n📊 순위 변화: {rank_change}" if rank_change else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"{CARD_NUM[i]} {pc}{pe} *[{persp}]* {category}{editor_mark}{watchlist_mark}\n"
            f"*{ticker}* ({full_name})  {arrow} *{p.get('change_24h','')}*  현재가 {p.get('current_price_krw','')}\n"
            f"_{description}_{unlock_line}{rank_line}\n\n"
            f"🏷️ 김프: {p.get('kimp','—')}  ({kimp_action})\n"
            f"🔗 BTC동조화: {p.get('btc_sync','—')}\n"
            f"🛡️ 방어력: {p.get('defense','—')}\n"
            f"📊 독자수급: {p.get('vol_divergence','—')}\n"
            f"📈 추세역행: {p.get('trend_reverse','—')}"
        )}})

        # ── 블록 2: 매매 셋업 + 손익비 + 권장비중 ──
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"🎯 *진입가* `{p.get('entry','—')}` _{p.get('entry_logic','')}_\n"
            f"🥇 *1차 목표* `{p.get('t1','—')}` *(+{t1_pct:.1f}%)* — 여기서 절반 매도\n"
            f"🏆 *2차 목표* `{p.get('t2','—')}` *(+{t2_pct:.1f}%)* — 나머지 홀딩\n"
            f"🛑 *손절가* `{p.get('stop_loss','—')}` *(-{sl_pct:.1f}%)* — 이 가격이면 포기\n"
            f"```\n{rr_text}\n```\n"
            f"💼 권장비중: {position_sz}"
        )}})

        # ── 블록 3: 이유 + 대응책 ──
        rel = "  ".join(f"`{c}`" for c in p.get("related_coins", []))
        rel_line = f"\n🔗 함께 볼 코인: {rel}" if rel else ""
        coin_narr = p.get("coin_narrative", "")
        coin_narr_line = f"\n\n📌 *개별 내러티브:* {coin_narr}" if coin_narr else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"⚠️ *왜 싼가?* {p.get('why_down','—')}\n\n"
            f"✅ *왜 사나?* {p.get('why_still','—')}\n\n"
            f"📉 기술신호: {p.get('tech_signal','—')}\n"
            f"🐋 큰손동향: {p.get('whale_signal','—')}\n\n"
            f"💡 *대응책*\n• 1차 목표 못 가면: {what_if_t1}\n• BTC 급락 시: {what_if_btc}"
            f"{coin_narr_line}"
            f"{rel_line}\n"
            f"⭐ {p.get('score','—')}  보유기간: {p.get('holding_period','—')}"
        )}})

        blocks.append({"type": "divider"})

    kw = "  ".join(data.get("keywords", []))
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": f"🌟 *트렌드 키워드*\n{kw}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "⚠️ 본 리포트는 투자 참고용이며, 투자 손실에 대한 책임은 본인에게 있습니다."}]})
    return blocks



# ==========================================
# 6. 메인 파이프라인
# ==========================================
def run_and_send_to_slack():
    print("🚀 차기 주도주 발굴 봇 v4 시작 (GitHub DB 연동)")
    print("=" * 60)

    # [2단계] 성과 추적
    perf_results = track_performance()
    perf_summary = performance_summary_text(perf_results)

    # [3단계] 복기
    review = run_ml_review(perf_results)

    # [4단계] 강화 규칙
    rules = update_rules(review)

    print("\n" + "=" * 60)

    # 데이터 수집
    target_coins, top30_coins, cutoff, coins_data = fetch_rising_star_bithumb_krw_coins()
    if not target_coins:
        print("❌ 빗썸 데이터 수집 실패"); return

    # 관심종목 추가
    if WATCHLIST:
        print(f"📌 관심종목 {len(WATCHLIST)}개 추가: {', '.join(WATCHLIST)}")
        existing_tickers = {t for t, _ in target_coins + top30_coins}
        for ticker in WATCHLIST:
            if ticker not in existing_tickers:
                # 빗썸에서 가격 가져오기
                try:
                    r = requests.get(f"https://api.bithumb.com/public/ticker/{ticker}_KRW", timeout=5)
                    d = r.json().get("data", {})
                    price  = float(d.get("closing_price", 0))
                    volume = float(d.get("acc_trade_value_24H", 0))
                    change = float(d.get("fluctate_rate_24H", 0))
                    if price > 0:
                        target_coins.append((ticker, {
                            "price": price, "volume": volume,
                            "change": change, "is_watchlist": True
                        }))
                        print(f"  ✅ 관심종목 {ticker} 추가 (현재가: {price:,}원)")
                except Exception as e:
                    print(f"  ⚠️ 관심종목 {ticker} 수집 실패: {e}")

    market_activity = fetch_market_activity(coins_data)
    print(f"✅ 시장: {market_activity['level_label']} | "
          f"메이저 {market_activity['major_ratio']}% / 알트 {market_activity['alt_ratio']}%")

    print("📡 BTC 1h 기준 데이터 수집 중...")
    btc_closes_1h, btc_vols_1h = fetch_btc_1h_base()

    all_coins  = target_coins + top30_coins
    indicators = fetch_indicators_for_top_coins(all_coins, btc_closes_1h, btc_vols_1h)

    # [1호출] 거시/코인니스 시장 내러티브
    market_narrative = fetch_market_narrative()
    if market_narrative:
        print("  ⏳ 다음 호출까지 15초 대기...")
        time.sleep(15)

    # [2호출] 개별 코인 촉매 분석
    coin_narratives = fetch_coin_narratives(target_coins, top30_coins)
    if coin_narratives:
        print("  ⏳ 다음 호출까지 15초 대기...")
        time.sleep(15)

    # [3호출] 종목 선별 (1+2 결과 주입)
    insights = generate_market_insights_via_gemini(
        target_coins, top30_coins, indicators, cutoff, market_activity,
        rules, perf_summary, market_narrative, coin_narratives
    )
    if not insights:
        print("❌ AI 분석 실패"); return

    pub_time = datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M")

    # [1단계] 추천 기록 저장 (GitHub)
    record_picks_to_github(insights.get("picks", []), market_activity, pub_time)

    # 내러티브 저장 (GitHub)
    save_narrative_to_github(market_narrative, coin_narratives, pub_time)

    # 슬랙 전송
    insights["market_narrative_data"] = market_narrative
    slack_blocks = build_slack_blocks(insights, pub_time, market_activity, perf_summary)
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=f"🚀 {pub_time} 차기 주도주 시그널 (TOP30 2종 + 급등후보 4종)",
            blocks=slack_blocks,
        )
        print("✅ 슬랙 전송 성공!")
    except SlackApiError as e:
        print(f"❌ 슬랙 오류: {e.response['error']}")
    except Exception as e:
        print(f"❌ 오류: {e}")

    picks = insights.get("picks", [])
    print("\n" + "=" * 60)
    print(
        f"✅ 완료\n"
        f"   추천 {len(picks)}종목 (TOP30 2 + 급등후보 4)\n"
        f"   성과추적 {len(perf_results)}건 처리\n"
        f"   복기 {'완료 (' + str(len(review.get('rule_improvements',[]))) + '개 규칙 도출)' if review else '없음 (누적 데이터 부족)'}\n"
        f"   강화규칙 {'적용 중' if rules else '없음'}"
    )

    # [HTML 생성 + GitHub Pages push]
    print("\n📄 HTML 리포트 생성 중...")
    html = generate_html_report(insights, pub_time, market_activity, perf_results, review, rules)
    push_report_to_github(html, pub_time)


# ==========================================
# 7. HTML 리포트 생성 + GitHub Pages push
# ==========================================

def generate_html_report(insights: dict, pub_time: str, market_activity: dict,
                          perf_results: list, review: dict, rules: str) -> str:
    """봇 실행 결과를 HTML 파일로 변환"""
    ma     = market_activity
    picks  = insights.get("picks", [])
    pct    = ma.get("vs_avg_pct", 0) or 0

    if   pct >= 200: strategy = "극도 과열 — 신규 진입 자제, 보유 익절 검토"
    elif pct >= 130: strategy = "수급 유입 중 — 모멘텀 코인 대응 가능"
    elif pct >= 70:  strategy = "평상시 수준 — 분할 매수 권장"
    elif pct >= 40:  strategy = "거래 한산 — 트리거 신호 대기 권장"
    else:            strategy = "극도 냉각 — 신규 진입 자제"

    PERSP_COLOR = {
        "TOP30 돌파":    "#8B5CF6",
        "TOP30 눌림목":  "#378ADD",
        "급등후보 돌파":  "#1D9E75",
        "급등후보 눌림목":"#F59E0B",
    }
    PERSP_EMOJI = {
        "TOP30 돌파":    "🏆",
        "TOP30 눌림목":  "🏛️",
        "급등후보 돌파":  "🚀",
        "급등후보 눌림목":"📉",
    }

    # ── 성과 섹션 HTML ───────────────────────────────────
    perf_html = ""
    if perf_results:
        wins  = [r for r in perf_results if "달성" in r["result"]]
        losses= [r for r in perf_results if "손절" in r["result"]]
        avg   = round(sum(r["pnl_pct"] for r in perf_results) / len(perf_results), 2)
        wr    = round(len(wins) / len(perf_results) * 100, 1)

        rows = ""
        for r in perf_results:
            color  = "#1D9E75" if r["pnl_pct"] >= 0 else "#E24B4A"
            badge  = ("✅ 달성" if "달성" in r["result"]
                      else "❌ 손절" if "손절" in r["result"]
                      else "📊 진행중")
            bcolor = ("#E1F5EE" if "달성" in r["result"]
                      else "#FCEBEB" if "손절" in r["result"]
                      else "#F1EFE8")
            btcolor= ("#0F6E56" if "달성" in r["result"]
                      else "#A32D2D" if "손절" in r["result"]
                      else "#5F5E5A")
            bar_w  = min(abs(r["pnl_pct"]) * 5, 100)
            rows += f"""
            <tr>
              <td style="font-weight:500;padding:10px 8px;">{r['ticker']}</td>
              <td><span style="background:{bcolor};color:{btcolor};padding:2px 8px;border-radius:6px;font-size:12px;">{badge}</span></td>
              <td style="padding:10px 8px;">
                <div style="display:flex;align-items:center;gap:8px;">
                  <div style="width:100px;background:#f0f0f0;border-radius:4px;height:6px;">
                    <div style="width:{bar_w}%;background:{color};height:6px;border-radius:4px;"></div>
                  </div>
                  <span style="color:{color};font-weight:500;">{r['pnl_pct']:+.1f}%</span>
                </div>
              </td>
            </tr>"""

        perf_html = f"""
        <div class="section">
          <h2>📊 성과 요약</h2>
          <div class="metric-grid">
            <div class="metric"><div class="metric-label">총 추천</div><div class="metric-value">{len(perf_results)}건</div></div>
            <div class="metric"><div class="metric-label">승률</div><div class="metric-value" style="color:#1D9E75;">{wr}%</div></div>
            <div class="metric"><div class="metric-label">평균 수익</div><div class="metric-value" style="color:{'#1D9E75' if avg>=0 else '#E24B4A'};">{avg:+.1f}%</div></div>
            <div class="metric"><div class="metric-label">T달성 / 손절</div><div class="metric-value">{len(wins)}건 / {len(losses)}건</div></div>
          </div>
          <table style="width:100%;border-collapse:collapse;margin-top:12px;">
            <thead><tr style="border-bottom:1px solid #eee;font-size:12px;color:#888;">
              <th style="text-align:left;padding:8px;">티커</th>
              <th style="text-align:left;padding:8px;">결과</th>
              <th style="text-align:left;padding:8px;">수익률</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    # ── 복기/강화규칙 섹션 HTML ──────────────────────────
    review_html = ""
    if review:
        rules_items = ""
        for item in review.get("rule_improvements", []):
            rules_items += f"""
            <div class="rule-item">
              <span class="rule-tag">{item.get('관점','공통')}</span>
              {item.get('개선규칙','')}
              <span style="font-size:11px;color:#aaa;margin-left:6px;">근거: {item.get('근거','')}</span>
            </div>"""

        review_html = f"""
        <div class="section">
          <h2>🔬 AI 복기 분석</h2>
          <div class="review-block">
            <div class="review-title">수익 종목 공통 패턴</div>
            <p>{review.get('win_pattern','—')}</p>
          </div>
          <div class="review-block">
            <div class="review-title">손절 종목 공통 패턴</div>
            <p>{review.get('lose_pattern','—')}</p>
          </div>
          <div class="review-block">
            <div class="review-title">다음 추천 핵심 포인트</div>
            <p>{review.get('next_focus','—')}</p>
          </div>
          {'<h3 style="margin-top:16px;font-size:14px;">현재 적용 중인 강화규칙</h3>' + rules_items if rules_items else ''}
        </div>"""

    # ── 종목 카드 HTML ───────────────────────────────────
    picks_html = ""
    editor_ticker = insights.get("editor_pick_ticker", "")
    for i, p in enumerate(picks):
        persp     = p.get("perspective", "")
        color     = PERSP_COLOR.get(persp, "#888")
        emoji     = PERSP_EMOJI.get(persp, "📌")
        t1_pct    = float(p.get("t1_pct") or 0)
        t2_pct    = float(p.get("t2_pct") or 0)
        sl_pct    = float(p.get("stop_loss_pct") or 0)

        # 가격 기반으로 직접 재계산
        try:
            entry_price = float(str(p.get("entry", 0)).replace(",", ""))
            t1_price    = float(str(p.get("t1", 0)).replace(",", ""))
            t2_price    = float(str(p.get("t2", 0)).replace(",", ""))
            sl_price    = float(str(p.get("stop_loss", 0)).replace(",", ""))
            if entry_price > 0:
                if t1_price > 0: t1_pct = round((t1_price - entry_price) / entry_price * 100, 1)
                if t2_price > 0: t2_pct = round((t2_price - entry_price) / entry_price * 100, 1)
                if sl_price > 0: sl_pct = round(abs((sl_price - entry_price) / entry_price * 100), 1)
        except Exception:
            pass

        rr        = round(t2_pct / sl_pct, 1) if sl_pct > 0 else 0
        chg       = float(p.get("change_num", 0) or 0)
        chg_color = "#1D9E75" if chg >= 0 else "#E24B4A"
        is_editor = p.get("ticker","") == editor_ticker
        unlock    = p.get("unlock_alert") or ""
        win_w     = min(int(t2_pct * 6), 200)
        lose_w    = min(int(sl_pct * 6), 120)

        editor_badge = '<span style="background:#FEF3C7;color:#92400E;padding:2px 8px;border-radius:6px;font-size:11px;margin-left:6px;">⭐ 편집장 픽</span>' if is_editor else ""
        unlock_box   = f'<div class="alert-box">🚨 <strong>이벤트 경보</strong> {unlock}</div>' if unlock else ""

        picks_html += f"""
        <div class="pick-card" style="border-left:4px solid {color};">
          <div class="pick-header">
            <div>
              <span class="persp-badge" style="background:{color}20;color:{color};">{emoji} {persp}</span>
              <span class="category-badge">{p.get('category','')}</span>
              {editor_badge}
            </div>
          </div>
          <div class="pick-title">
            <span class="ticker">{p.get('ticker','')}</span>
            <span class="full-name">{p.get('ticker_full_name','')}</span>
            <span class="price">{p.get('current_price_krw','')}</span>
            <span style="color:{chg_color};font-weight:500;">{p.get('change_24h','')}</span>
          </div>
          <p style="font-size:13px;color:#666;margin:4px 0 12px;">{p.get('ticker_description','')}</p>
          {unlock_box}

          <div class="trade-grid">
            <div class="trade-box">
              <div class="trade-label">🎯 진입가</div>
              <div class="trade-value">{p.get('entry','—')}</div>
              <div class="trade-sub">{p.get('entry_logic','')}</div>
            </div>
            <div class="trade-box" style="border-color:#1D9E75;">
              <div class="trade-label">🥇 1차 목표 (절반 매도)</div>
              <div class="trade-value" style="color:#1D9E75;">{p.get('t1','—')}</div>
              <div class="trade-sub">+{t1_pct:.1f}%</div>
            </div>
            <div class="trade-box" style="border-color:#1D9E75;">
              <div class="trade-label">🏆 2차 목표 (나머지 홀딩)</div>
              <div class="trade-value" style="color:#1D9E75;">{p.get('t2','—')}</div>
              <div class="trade-sub">+{t2_pct:.1f}%</div>
            </div>
            <div class="trade-box" style="border-color:#E24B4A;">
              <div class="trade-label">🛑 손절가 (이 가격이면 포기)</div>
              <div class="trade-value" style="color:#E24B4A;">{p.get('stop_loss','—')}</div>
              <div class="trade-sub">-{sl_pct:.1f}%</div>
            </div>
          </div>

          <div class="rr-section">
            <div class="rr-label-row">
              <span style="font-size:12px;color:#666;">⚖️ 손익비 — 얼마나 유리한 거래인가</span>
              <span class="rr-verdict" style="color:{'#1D9E75' if rr>=2 else '#F59E0B' if rr>=1.5 else '#E24B4A'};">
                {'✅ 유리' if rr>=2 else '⚠️ 보통' if rr>=1.5 else '❌ 불리'} {rr}:1
              </span>
            </div>
            <div style="margin:6px 0 2px;">
              <span style="font-size:11px;color:#888;display:inline-block;width:70px;">벌 수 있음</span>
              <div style="display:inline-block;width:{win_w}px;height:8px;background:#1D9E75;border-radius:4px;vertical-align:middle;"></div>
              <span style="font-size:12px;color:#1D9E75;margin-left:6px;">최대 +{t2_pct:.1f}%</span>
            </div>
            <div>
              <span style="font-size:11px;color:#888;display:inline-block;width:70px;">잃을 수 있음</span>
              <div style="display:inline-block;width:{lose_w}px;height:8px;background:#E24B4A;border-radius:4px;vertical-align:middle;"></div>
              <span style="font-size:12px;color:#E24B4A;margin-left:6px;">최대 -{sl_pct:.1f}%</span>
            </div>
          </div>

          <div class="info-grid">
            <div><span class="info-label">🏷️ 김프</span><span>{p.get('kimp','—')}</span><br><span style="font-size:11px;color:#888;">{p.get('kimp_action','')}</span></div>
            <div><span class="info-label">🔗 BTC 동조화</span><span>{p.get('btc_sync','—')}</span></div>
            <div><span class="info-label">🛡️ 방어력</span><span>{p.get('defense','—')}</span></div>
            <div><span class="info-label">📊 독자 수급</span><span>{p.get('vol_divergence','—')}</span></div>
          </div>

          <div class="reason-grid">
            <div class="reason-box">
              <div class="reason-title">⚠️ 왜 지금 싸게 거래되나?</div>
              <p>{p.get('why_down','—')}</p>
            </div>
            <div class="reason-box">
              <div class="reason-title">✅ 그럼에도 지금 사는 이유</div>
              <p>{p.get('why_still','—')}</p>
            </div>
          </div>

          <div class="whatif-box">
            <strong>💡 예상과 다를 때 대응책</strong><br>
            • 1차 목표 못 가면: {p.get('what_if_t1_miss','—')}<br>
            • 비트코인 급락 시: {p.get('what_if_btc_drop','—')}
          </div>

          <div style="margin-top:12px;font-size:12px;color:#888;">
            💼 권장 비중: {p.get('position_size','—')} &nbsp;|&nbsp;
            ⏱️ 보유기간: {p.get('holding_period','—')} &nbsp;|&nbsp;
            {p.get('score','—')}
          </div>
        </div>"""

    # ── 전체 HTML 조립 ───────────────────────────────────
    mood         = insights.get("market_mood", "")
    editor_reason= insights.get("editor_pick_reason","")
    keywords     = "  ".join(f'<span class="kw-badge">{k}</span>' for k in insights.get("keywords",[]))

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>차기 주도주 리포트 — {pub_time}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f5f5f5; color: #1a1a1a; line-height: 1.6; }}
  .container {{ max-width: 900px; margin: 0 auto; padding: 24px 16px; }}
  .report-header {{ background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 16px;
                    border: 1px solid #e5e5e5; }}
  .report-title {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .report-sub {{ font-size: 13px; color: #888; }}
  .temp-badge {{ display:inline-block; padding:4px 12px; border-radius:20px;
                 background:#FEF3C7; color:#92400E; font-size:12px; font-weight:500; margin-top:8px; }}
  .section {{ background:#fff; border-radius:12px; padding:20px 24px;
              margin-bottom:16px; border:1px solid #e5e5e5; }}
  .section h2 {{ font-size:16px; font-weight:600; margin-bottom:16px; }}
  .metric-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }}
  .metric {{ background:#f8f8f8; border-radius:8px; padding:12px; }}
  .metric-label {{ font-size:11px; color:#888; margin-bottom:4px; }}
  .metric-value {{ font-size:20px; font-weight:600; }}
  .strategy-box {{ background:#FFFBEB; border:1px solid #FDE68A; border-radius:8px;
                   padding:12px 16px; font-size:13px; color:#92400E; margin-top:12px; }}
  .editor-pick {{ background:#F0FDF4; border:1px solid #BBF7D0; border-radius:8px;
                  padding:14px 16px; margin-bottom:16px; }}
  .pick-card {{ background:#fff; border-radius:12px; padding:20px 24px;
                margin-bottom:14px; border:1px solid #e5e5e5; }}
  .pick-header {{ display:flex; align-items:center; justify-content:space-between;
                  margin-bottom:8px; flex-wrap:wrap; gap:6px; }}
  .persp-badge {{ display:inline-block; padding:3px 10px; border-radius:20px;
                  font-size:12px; font-weight:500; }}
  .category-badge {{ display:inline-block; background:#f0f0f0; color:#555;
                     padding:3px 10px; border-radius:20px; font-size:12px; margin-left:6px; }}
  .pick-title {{ display:flex; align-items:baseline; gap:8px; margin:8px 0 4px; flex-wrap:wrap; }}
  .ticker {{ font-size:22px; font-weight:700; }}
  .full-name {{ font-size:13px; color:#888; }}
  .price {{ font-size:18px; font-weight:500; margin-left:4px; }}
  .trade-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
                 gap:10px; margin:14px 0; }}
  .trade-box {{ background:#fafafa; border:1px solid #e5e5e5; border-radius:8px; padding:12px; }}
  .trade-label {{ font-size:11px; color:#888; margin-bottom:4px; }}
  .trade-value {{ font-size:16px; font-weight:600; }}
  .trade-sub {{ font-size:11px; color:#aaa; margin-top:2px; }}
  .rr-section {{ background:#fafafa; border-radius:8px; padding:12px 14px; margin:12px 0; }}
  .rr-label-row {{ display:flex; justify-content:space-between; align-items:center;
                   margin-bottom:8px; flex-wrap:wrap; gap:6px; }}
  .rr-verdict {{ font-size:13px; font-weight:600; }}
  .info-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
                gap:10px; margin:14px 0; font-size:13px; }}
  .info-label {{ font-weight:500; margin-right:4px; display:block; font-size:11px;
                 color:#888; margin-bottom:2px; }}
  .reason-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:12px 0; }}
  @media(max-width:600px) {{ .reason-grid {{ grid-template-columns:1fr; }} }}
  .reason-box {{ background:#fafafa; border-radius:8px; padding:12px; font-size:13px; }}
  .reason-title {{ font-weight:600; font-size:12px; margin-bottom:6px; color:#555; }}
  .whatif-box {{ background:#EFF6FF; border-radius:8px; padding:12px 14px;
                 font-size:13px; color:#1e40af; margin-top:10px; line-height:1.8; }}
  .alert-box {{ background:#FEF2F2; border:1px solid #FECACA; border-radius:8px;
                padding:10px 14px; font-size:13px; color:#991B1B; margin:10px 0; }}
  .review-block {{ background:#f8f8f8; border-radius:8px; padding:12px 14px; margin-bottom:10px; }}
  .review-title {{ font-weight:600; font-size:13px; margin-bottom:6px; }}
  .review-block p {{ font-size:13px; color:#555; }}
  .rule-item {{ padding:10px 0; border-bottom:1px solid #eee; font-size:13px; }}
  .rule-item:last-child {{ border-bottom:none; }}
  .rule-tag {{ background:#f0f0f0; color:#555; padding:2px 8px; border-radius:6px;
               font-size:11px; margin-right:6px; }}
  .kw-badge {{ background:#EFF6FF; color:#1D4ED8; padding:3px 10px;
               border-radius:20px; font-size:12px; margin:2px; display:inline-block; }}
  .nav-bar {{ background:#fff; border-radius:12px; padding:12px 16px; margin-bottom:16px;
              border:1px solid #e5e5e5; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .nav-bar span {{ font-size:12px; color:#888; margin-right:4px; }}
  .nav-btn {{ padding:5px 14px; border-radius:20px; border:1px solid #e5e5e5;
              font-size:12px; cursor:pointer; background:#fff; color:#333;
              text-decoration:none; transition:all 0.15s; }}
  .nav-btn:hover {{ background:#f0f0f0; }}
  .nav-btn.active {{ background:#1a1a1a; color:#fff; border-color:#1a1a1a; }}
  @media(max-width:600px) {{
    .trade-grid {{ grid-template-columns:1fr 1fr; }}
    .info-grid  {{ grid-template-columns:1fr 1fr; }}
  }}
</style>
</head>
<body>
<div class="container">

  <!-- 헤더 -->
  <div class="report-header">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
      <div>
        <p style="font-size:12px;color:#888;margin-bottom:4px;">차기 주도주 리포트</p>
        <div class="report-title">🚀 {pub_time}</div>
        <div class="report-sub">빗썸 31~80위 스캔 · Gemini AI · BTC 동조화 · 김프</div>
        <span class="temp-badge">{ma.get('level_label','—')}</span>
      </div>
    </div>
  </div>

  <!-- 시장 온도계 -->
  <div class="section">
    <h2>🌡️ 시장 온도계</h2>
    <div class="metric-grid">
      <div class="metric">
        <div class="metric-label">전체 거래대금</div>
        <div class="metric-value">{ma['total_volume']/1e12:.2f}조원</div>
        <div style="font-size:11px;color:#888;margin-top:2px;">전월 평균 대비 {ma.get('vs_avg_pct','—')}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">수급 주도</div>
        <div class="metric-value" style="font-size:16px;">{ma.get('supply_character','—')}</div>
      </div>
      <div class="metric">
        <div class="metric-label">메이저 / 알트</div>
        <div class="metric-value" style="font-size:16px;">{ma['major_ratio']}% / {ma['alt_ratio']}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">시장 분위기</div>
        <div class="metric-value" style="font-size:13px;line-height:1.4;">{mood[:40] + '...' if len(mood) > 40 else mood}</div>
      </div>
    </div>
    <div class="strategy-box">📌 오늘의 전략: {strategy}</div>
  </div>

  <!-- 편집장 픽 -->
  {f'''<div class="editor-pick">
    ⭐ <strong>편집장 픽 — 오늘 1종목만 골라야 한다면?</strong><br>
    <span style="font-size:15px;font-weight:600;">{editor_ticker}</span>
    <span style="font-size:13px;color:#555;margin-left:8px;">{editor_reason}</span>
  </div>''' if editor_ticker else ''}

  <!-- 종목 분석 -->
  <div class="section">
    <h2>🎯 종목 분석 ({len(picks)}종목 — TOP30 2종 + 급등후보 4종)</h2>
  </div>
  {picks_html}

  <!-- 성과 -->
  {perf_html}

  <!-- 복기 -->
  {review_html}

  <!-- 키워드 -->
  <div class="section">
    <h2>🌟 트렌드 키워드</h2>
    <div>{keywords}</div>
  </div>

  <p style="text-align:center;font-size:12px;color:#aaa;padding:20px 0;">
    ⚠️ 본 리포트는 투자 참고용이며, 투자 손실에 대한 책임은 본인에게 있습니다.
  </p>
</div>
</body>
</html>"""
    return html


def push_report_to_github(html_content: str, pub_time: str):
    """HTML 리포트를 GitHub Pages에 push"""
    try:
        import base64

        token   = GITHUB_TOKEN.strip()
        repo    = "insight-doby/report_coin"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        # 날짜별 파일명 (예: report_20260601_1057.html)
        file_date = datetime.now(KST).strftime("%Y%m%d_%H%M")
        filename  = f"reports/report_{file_date}.html"

        # reports/ 폴더에 날짜별로만 쌓기 (index.html은 건드리지 않음)
        files_to_push = [
            (filename, html_content),
        ]

        for path, content in files_to_push:
            url     = f"https://api.github.com/repos/{repo}/contents/{path}"
            encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

            # 파일이 이미 있으면 sha 가져오기 (덮어쓰기용)
            sha = None
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                sha = r.json().get("sha")

            body = {
                "message": f"리포트 업데이트: {pub_time}",
                "content": encoded,
            }
            if sha:
                body["sha"] = sha

            r = requests.put(url, headers=headers, json=body, timeout=30)
            if r.status_code in (200, 201):
                print(f"  ✅ {path} push 완료")
            else:
                print(f"  ❌ {path} push 실패: {r.status_code} {r.text[:100]}")

        # 히스토리 인덱스 페이지 생성/업데이트
        _update_history_index(headers, repo, file_date, pub_time)

        print(f"🌐 리포트 URL: https://insight-doby.github.io/report_coin/")
        print(f"📁 날짜별 URL: https://insight-doby.github.io/report_coin/{filename}")

    except Exception as e:
        print(f"  ❌ GitHub push 오류: {e}")


def _update_history_index(headers: dict, repo: str, file_date: str, pub_time: str):
    """히스토리 목록 페이지(history.html) 업데이트"""
    try:
        import base64

        url = f"https://api.github.com/repos/{repo}/contents/history.html"

        # 기존 history.html 가져오기
        r = requests.get(url, headers=headers, timeout=10)
        existing_sha  = None
        existing_rows = ""

        if r.status_code == 200:
            existing_sha  = r.json().get("sha")
            old_html      = base64.b64decode(r.json()["content"]).decode("utf-8")
            # 기존 행 추출
            start = old_html.find('<tbody>') + 7
            end   = old_html.find('</tbody>')
            if start > 6 and end > 0:
                existing_rows = old_html[start:end]

        new_row = f"""
        <tr data-date="{pub_time}" data-file="reports/report_{file_date}.html">
          <td style="padding:10px 12px;font-size:13px;">{pub_time}</td>
          <td style="padding:10px 12px;">
            <a href="reports/report_{file_date}.html"
               style="color:#2563eb;text-decoration:none;font-size:13px;font-weight:500;">리포트 보기</a>
          </td>
          <td style="padding:10px 12px;">
            <button onclick="deleteRow(this, '{pub_time}', 'reports/report_{file_date}.html')"
              style="padding:3px 10px;font-size:11px;font-weight:600;border:1px solid #e5e5e5;
              border-radius:4px;background:transparent;color:#aaa;cursor:pointer;"
              onmouseover="this.style.borderColor='#ef4444';this.style.color='#ef4444';"
              onmouseout="this.style.borderColor='#e5e5e5';this.style.color='#aaa';">
              X
            </button>
          </td>
        </tr>"""

        history_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>리포트 히스토리</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#f5f5f5; padding:24px; color:#18181b; }}
  .container {{ max-width:700px; margin:0 auto; }}
  .back-btn {{ display:inline-block; margin-bottom:16px; color:#2563eb;
               text-decoration:none; font-size:13px; }}
  h1 {{ font-size:18px; font-weight:700; margin-bottom:16px; letter-spacing:-0.02em; }}
  table {{ width:100%; background:#fff; border-radius:10px;
           border-collapse:collapse; border:1px solid #e5e5e5; }}
  thead tr {{ background:#f8f8f8; border-bottom:1px solid #e5e5e5; }}
  th {{ padding:10px 12px; text-align:left; font-size:11px; color:#a1a1aa;
        font-weight:600; text-transform:uppercase; letter-spacing:0.06em; }}
  tbody tr {{ border-bottom:1px solid #f0f0f0; }}
  tbody tr:last-child {{ border-bottom:none; }}
  tbody tr:hover {{ background:#fafafa; }}
  .notice {{ font-size:12px; color:#a1a1aa; margin-top:12px; }}
</style>
</head>
<body>
<div class="container">
  <a href="index.html" class="back-btn">← 최신 리포트로</a>
  <h1>리포트 히스토리</h1>
  <table>
    <thead><tr><th>발행일시</th><th>링크</th><th></th></tr></thead>
    <tbody id="tbody">{new_row}{existing_rows}</tbody>
  </table>
  <p class="notice">행 삭제는 히스토리 목록에서만 제거됩니다. 실제 리포트 파일은 유지됩니다.</p>
</div>
<script>
function deleteRow(btn, date, file) {{
  if(!confirm('"' + date + '" 항목을 목록에서 삭제할까요?')) return;
  var row = btn.closest('tr');
  if(row) row.remove();
  /* 남은 행 수집해서 localStorage에 숨김 처리 */
  var hidden = JSON.parse(localStorage.getItem('kzb-hidden-reports') || '[]');
  hidden.push(file);
  localStorage.setItem('kzb-hidden-reports', JSON.stringify(hidden));
}}
/* 페이지 로드 시 숨김 처리된 행 제거 */
document.addEventListener('DOMContentLoaded', function() {{
  var hidden = JSON.parse(localStorage.getItem('kzb-hidden-reports') || '[]');
  document.querySelectorAll('#tbody tr').forEach(function(row) {{
    var file = row.dataset.file;
    if(file && hidden.indexOf(file) !== -1) row.remove();
  }});
}});
</script>
</body>
</html>"""

        encoded = base64.b64encode(history_html.encode("utf-8")).decode("utf-8")
        body = {"message": f"히스토리 업데이트: {pub_time}", "content": encoded}
        if existing_sha:
            body["sha"] = existing_sha

        r = requests.put(url, headers=headers, json=body, timeout=30)
        if r.status_code in (200, 201):
            print("  ✅ history.html 업데이트 완료")
    except Exception as e:
        print(f"  ⚠️ 히스토리 업데이트 실패: {e}")


if __name__ == "__main__":
    run_and_send_to_slack()
