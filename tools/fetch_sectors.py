"""CoinGecko 카테고리 → 빗썸 원화 종목 매핑 + 빗썸 1시간봉(최근 3일) → data/sectors.json"""
import json, time, requests
S = requests.Session()
def get(url, params=None, tries=5):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200: return r.json()
            print("HTTP", r.status_code, url, params); time.sleep(15 * (i + 1))
        except Exception as e:
            print("ERR", e); time.sleep(10)
    return None
CATS = {
 "sui-ecosystem": "수이", "solana-ecosystem": "솔라나", "base-ecosystem": "베이스", "the-open-network-ecosystem": "톤",
 "aptos-ecosystem": "앱토스", "binance-smart-chain": "BNB체인", "arbitrum-ecosystem": "아비트럼", "optimism-ecosystem": "옵티미즘",
 "avalanche-ecosystem": "아발란체", "cosmos-ecosystem": "코스모스", "polkadot-ecosystem": "폴카닷", "near-protocol-ecosystem": "니어",
 "tron-ecosystem": "트론", "berachain-ecosystem": "베라체인", "sei-ecosystem": "세이", "hyperliquid-ecosystem": "하이퍼리퀴드",
 "artificial-intelligence": "AI", "ai-agents": "AI에이전트", "meme-token": "밈", "real-world-assets-rwa": "RWA",
 "layer-1": "레이어1", "layer-2": "레이어2", "gaming": "게임", "decentralized-finance-defi": "디파이", "depin": "디핀",
 "zero-knowledge-zk": "ZK", "privacy-coins": "프라이버시", "storage": "스토리지", "oracle": "오라클", "interoperability": "인터체인",
}
tick = get("https://api.bithumb.com/public/ticker/ALL_KRW")["data"]
krw = {k for k in tick if k != "date"}
names = {m["market"][4:]: m.get("english_name", "") for m in get("https://api.bithumb.com/v1/market/all", {"isDetails": "false"}) if m["market"].startswith("KRW-")}
members = {}
for cid, label in CATS.items():
    got = []
    for page in (1, 2):
        j = get("https://api.coingecko.com/api/v3/coins/markets", {"vs_currency": "usd", "category": cid, "per_page": 250, "page": page})
        time.sleep(7)
        if not j: break
        got += j
        if len(j) < 250: break
    m = []
    for c in got:
        sym = (c.get("symbol") or "").upper()
        if sym in krw:
            # 같은 심볼 다른 코인 오매칭 방지: 이름 첫 단어 비교(빗썸 영문명 있으면)
            en = (names.get(sym) or "").lower().split(" ")[0]
            if en and en[:4] not in (c.get("name") or "").lower() and en[:4] not in (c.get("id") or ""):
                continue
            m.append({"tk": sym, "mcap": c.get("market_cap"), "rank": c.get("market_cap_rank")})
    members[label] = m
    print(label, len(got), "→ 빗썸", len(m), [x["tk"] for x in m][:15])
def hours(tk):
    rows, to = [], None
    j = get("https://api.bithumb.com/v1/candles/minutes/60", {"market": f"KRW-{tk}", "count": 72}, tries=2)
    return [[r["candle_date_time_kst"][:13], r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], round(r["candle_acc_trade_price"])] for r in reversed(j or [])]
allm = sorted({x["tk"] for v in members.values() for x in v} | {"BTC"})
h1 = {}
for tk in allm:
    h1[tk] = hours(tk); time.sleep(0.05)
json.dump({"members": members, "h1": h1, "tick": {k: tick[k] for k in allm if k in tick}}, open("data/sectors.json", "w"), ensure_ascii=False, separators=(",", ":"))
print("done", len(allm))
