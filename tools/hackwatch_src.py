import re, requests
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
base = "https://binance-hack-watch.vercel.app"
html = S.get(base + "/", timeout=20).text
js = re.findall(r'(?:src|href)="(/[^"]+\.js)"', html)
blob = html + "\n".join(S.get(base + j, timeout=20).text for j in js)
out = []
for kw in ("Exploiter", "blockscout", "search", "fetch(", "sold", "tokens", "ENA", "tag"):
    for m in list(re.finditer(re.escape(kw), blob))[:6]:
        out.append(f"--- {kw} @{m.start()}: " + blob[max(0, m.start() - 200): m.start() + 300].replace("\n", " "))
open("data/hackwatch_src.txt", "w").write(f"html {len(html)} js {js}\n" + "\n".join(out))
