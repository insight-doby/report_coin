"""바이낸스 해커 지갑 추적: 대시보드 소스에서 지갑 주소를 찾고 Blockscout로 토큰 잔고 조회 → data/hackwatch.json/.txt"""
import re, json, time, requests
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "src": [], "addresses": {}, "log": []}
base = "https://binance-hack-watch.vercel.app/"
try:
    html = S.get(base, timeout=20).text
    js = re.findall(r'src="(/[^"]+\.js)"', html) + re.findall(r'href="(/[^"]+\.js)"', html)
    texts = [html] + [S.get(base.rstrip("/") + j, timeout=20).text for j in js[:30]]
    blob = "\n".join(texts)
    addrs = sorted(set(re.findall(r"0x[a-fA-F0-9]{40}", blob)))
    apis = sorted(set(re.findall(r"https?://[a-zA-Z0-9.\-]+(?:/[a-zA-Z0-9_\-./{}?=&]*)?", blob)))
    out["src"] = [a for a in apis if "blockscout" in a or "api" in a][:40]
    out["log"].append(f"js {len(js)} addrs {len(addrs)}")
    # 주소 주변 문맥(라벨)
    for a in addrs:
        i = blob.find(a); ctx = blob[max(0, i - 120): i + 60].replace("\n", " ")
        out["addresses"][a] = {"ctx": ctx[-180:]}
except Exception as e:
    out["log"].append(f"page error {e}")
TOK = {"ENA", "PEPE", "LINK", "AAVE", "UNI", "ARB", "ONDO", "WLD", "FUN", "ZAMA", "ZRO", "RSR", "WETH", "USDT", "USDC"}
for a in list(out["addresses"])[:25]:
    try:
        r = S.get(f"https://eth.blockscout.com/api/v2/addresses/{a}", timeout=20).json()
        out["addresses"][a]["name"] = (r.get("name") or "") + " " + ",".join(t.get("display_name", "") for t in (r.get("public_tags") or r.get("metadata", {}).get("tags", []) or []) if isinstance(t, dict))
        out["addresses"][a]["eth"] = int(r.get("coin_balance") or 0) / 1e18
        tb = S.get(f"https://eth.blockscout.com/api/v2/addresses/{a}/token-balances", timeout=20).json()
        hold = {}
        for t in tb if isinstance(tb, list) else []:
            sym = (t.get("token") or {}).get("symbol"); dec = int((t.get("token") or {}).get("decimals") or 18)
            if sym in TOK:
                hold[sym] = int(t.get("value") or 0) / 10 ** dec
        out["addresses"][a]["tokens"] = hold
        time.sleep(0.3)
    except Exception as e:
        out["addresses"][a]["err"] = str(e)[:80]
json.dump(out, open("data/hackwatch.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False)[:3000])
