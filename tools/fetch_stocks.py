"""한국·미국 대형주 일봉 (2020~) → data/stocks_d1.json.gz  (코인과 같은 지표 검증용)
각 행: [날짜, 시가, 고가, 저가, 종가, 거래대금(현지통화)]"""
import subprocess, sys, json, gzip
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance"], check=False)
import yfinance as yf
KR = ["005930", "000660", "373220", "207940", "005380", "000270", "068270", "005490", "035420", "105560",
      "055550", "012330", "028260", "006400", "051910", "035720", "003670", "066570", "096770", "032830",
      "086790", "015760", "034730", "017670", "033780", "018260", "011200", "010130", "009150", "003550",
      "042700", "012450", "047810", "010140", "329180", "064350", "000810", "024110", "316140", "036570"]
KQ = ["247540", "086520", "196170", "028300", "058470", "277810", "145020", "403870", "263750", "293490"]
US = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "UNH", "XOM", "LLY", "MA", "HD",
      "COST", "PG", "JNJ", "NFLX", "AMD", "CRM", "ORCL", "ADBE", "KO", "PEP", "BAC", "WMT", "INTC", "QCOM", "MU",
      "PLTR", "COIN", "MSTR", "SHOP", "UBER", "SMCI", "ARM", "CRWD", "SNOW", "SQ"]
IDX = {"^KS11": "KOSPI", "^GSPC": "SPX"}
tickers = [t + ".KS" for t in KR] + [t + ".KQ" for t in KQ] + US + list(IDX)
df = yf.download(tickers, start="2020-10-01", auto_adjust=True, group_by="ticker", progress=False, threads=True)
out = {}
for t in tickers:
    try:
        d = df[t].dropna()
    except KeyError:
        continue
    if len(d) < 200:
        continue
    name = IDX.get(t, t)
    out[name] = [[i.strftime("%Y-%m-%d"), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Close * r.Volume)]
                 for i, r in d.iterrows()]
with gzip.open("data/stocks_d1.json.gz", "wt", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"))
print("stocks", len(out), {k: (v[0][0], v[-1][0]) for k, v in out.items() if k in ("KOSPI", "SPX", "AAPL", "005930.KS")})
