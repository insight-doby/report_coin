"""스캔 요약을 텔레그램용 이미지 카드(PNG)로 그린다."""
import glob
import io
import os

from PIL import Image, ImageDraw, ImageFont

W = 1080
PAD = 56
BG = (15, 20, 30)
PANEL = (26, 33, 46)
LINE = (44, 53, 70)
TXT = (236, 240, 246)
SUB = (148, 160, 180)
DIM = (100, 112, 132)
GREEN = (52, 199, 120)
YELLOW = (245, 190, 60)
RED = (240, 82, 82)
BLUE = (90, 150, 255)
GATE = {"GO": GREEN, "SELECTIVE": YELLOW, "STOP": RED}


def _font_path(bold: bool) -> str:
    want = "Bold" if bold else "Regular"
    pats = [f"/usr/share/fonts/opentype/noto/NotoSansCJK-{want}.ttc",
            f"/usr/share/fonts/**/NotoSansCJK*{want}*", f"/usr/share/fonts/**/Nanum*{'Bold' if bold else ''}*.ttf",
            "/usr/share/fonts/**/NotoSansCJK*", "/usr/share/fonts/**/Nanum*.ttf"]
    for p in pats:
        hit = sorted(glob.glob(p, recursive=True))
        if hit:
            return hit[0]
    raise FileNotFoundError("한글 폰트 없음 (fonts-noto-cjk 설치 필요)")


_cache = {}


def F(size: int, bold: bool = False):
    k = (size, bold)
    if k not in _cache:
        path = _font_path(bold)
        idx = 1 if path.endswith(".ttc") else 0          # ttc 1번 = KR
        try:
            _cache[k] = ImageFont.truetype(path, size, index=idx)
        except Exception:
            _cache[k] = ImageFont.truetype(path, size)
    return _cache[k]


def _krw(x) -> str:
    if x is None:
        return "-"
    if x >= 100:
        return f"{x:,.0f}원"
    if x >= 1:
        return f"{x:,.2f}원"
    return f"{x:.4f}원"


def _wrap(d, text, font, width):
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) > width and cur:
            cut = cur.rfind(" ")
            if cut > len(cur) * 0.5:
                lines.append(cur[:cut])
                cur = cur[cut + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W, 4000), BG)
        self.d = ImageDraw.Draw(self.img)
        self.y = PAD

    def text(self, x, s, size, color=TXT, bold=False, y=None, anchor="la"):
        self.d.text((x, self.y if y is None else y), s, font=F(size, bold), fill=color, anchor=anchor)

    def panel(self, h, color=PANEL):
        self.d.rounded_rectangle((PAD, self.y, W - PAD, self.y + h), 28, fill=color)

    def section(self, title, right=""):
        self.y += 28
        self.text(PAD + 4, title, 30, SUB, True)
        if right:
            self.text(W - PAD - 4, right, 26, DIM, y=self.y + 4, anchor="ra")
        self.y += 54

    def mark(self, x, y, ok):
        c = GREEN if ok else YELLOW
        self.d.ellipse((x, y, x + 30, y + 30), fill=c)
        if ok:
            self.d.line((x + 8, y + 16, x + 13, y + 21, x + 23, y + 10), fill=BG, width=4)
        else:
            self.d.rectangle((x + 13, y + 7, x + 17, y + 18), fill=BG)
            self.d.rectangle((x + 13, y + 21, x + 17, y + 24), fill=BG)

    def done(self) -> bytes:
        self.y += PAD
        buf = io.BytesIO()
        self.img.crop((0, 0, W, self.y)).save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _pick_card(c: Canvas, p: dict, name: str):
    h = 335
    c.panel(h)
    x0, y0 = PAD + 36, c.y + 30
    now = p["state"] == "now"
    c.text(x0, name, 40, TXT, True, y=y0)
    tw = c.d.textlength(name, font=F(40, True))
    c.text(x0 + tw + 16, p["ticker"], 26, DIM, y=y0 + 12)
    tag = "지금 매수 구간" if now else "지정가 대기"
    tc = GREEN if now else BLUE
    tl = c.d.textlength(tag, font=F(24, True)) + 36
    c.d.rounded_rectangle((W - PAD - 36 - tl, y0 + 2, W - PAD - 36, y0 + 46), 22, outline=tc, width=2)
    c.text(W - PAD - 36 - tl / 2, tag, 24, tc, True, y=y0 + 24, anchor="mm")
    c.text(x0, f"현재 {_krw(p['price'])} · {p.get('setup_label', '')}", 28, SUB, y=y0 + 62)

    # 가격 막대: 손절 ── 매수 구간 ── 1차 ── 2차
    bx0, bx1, by = x0, W - PAD - 36, y0 + 150
    lo, hi = p["stop"], p["t2"]
    pos = lambda v: bx0 + (bx1 - bx0) * (v - lo) / (hi - lo or 1)
    c.d.rounded_rectangle((bx0, by - 5, bx1, by + 5), 5, fill=LINE)
    c.d.rounded_rectangle((pos(p["zone_lo"]), by - 9, pos(p["zone_hi"]), by + 9), 9, fill=GREEN)
    for v, col in ((p["stop"], RED), (p["t1"], BLUE), (p["t2"], BLUE)):
        c.d.ellipse((pos(v) - 9, by - 9, pos(v) + 9, by + 9), fill=col)
    px = max(bx0, min(bx1, pos(p["price"])))
    c.d.polygon([(px - 11, by - 26), (px + 11, by - 26), (px, by - 12)], fill=TXT)

    cols = [("손절", _krw(p["stop"]), f"-{p['stop_pct']:.1f}%", RED),
            ("매수 구간", f"{_krw(p['zone_lo'])}~", _krw(p["zone_hi"]), GREEN),
            ("1차 목표", _krw(p["t1"]), f"+{p['t1_pct']:.1f}%", BLUE),
            ("2차 목표", _krw(p["t2"]), f"+{p['t2_pct']:.1f}%", BLUE)]
    cw = (bx1 - bx0) / 4
    for i, (k, v, s, col) in enumerate(cols):
        cx = bx0 + cw * i
        c.text(cx, k, 22, DIM, y=by + 26)
        c.text(cx, v, 26, TXT, True, y=by + 54)
        c.text(cx, s, 22, col, y=by + 88)
    c.y += h + 20


def scan_card(latest: dict, names=None) -> bytes:
    names = names or {}
    nm = lambda t: names.get(t) or latest.get("names", {}).get(t) or t
    m = latest["market"]
    col = GATE.get(m["gate"], SUB)
    picks = latest.get("picks") or []
    c = Canvas()

    # ── 헤더 ──
    when = latest["generated_at"][5:16].replace("T", " ").replace("-", "/")
    c.text(PAD + 4, f"코인 스캔 · {when}", 28, SUB)
    c.text(W - PAD - 4, "2~5일 스윙", 28, DIM, anchor="ra")
    c.y += 60
    h = 250
    c.panel(h)
    c.d.rounded_rectangle((PAD, c.y, PAD + 14, c.y + h), 7, fill=col)
    cx, cy = PAD + 100, c.y + 92
    c.d.ellipse((cx - 44, cy - 44, cx + 44, cy + 44), fill=col)
    c.d.ellipse((cx - 64, cy - 64, cx + 64, cy + 64), outline=col, width=4)
    c.text(PAD + 196, m["label"], 60, col, True, y=c.y + 40)
    c.text(PAD + 196, f"추천 {len(picks)}개  ·  권장 비중 ×{m['size_mult']:g}", 30, TXT, y=c.y + 122)
    hl = _wrap(c.d, m.get("headline", ""), F(28), W - PAD * 2 - 80)
    for i, ln in enumerate(hl[:2]):
        c.text(PAD + 40, ln, 28, SUB, y=c.y + 180 + i * 38)
    c.y += h + (38 if len(hl) > 1 else 0) + 10

    # ── 추천 ──
    if picks:
        now = sum(1 for p in picks if p["state"] == "now")
        c.section("추천 종목", f"매수 구간 {now} · 대기 {len(picks) - now}")
        for p in picks:
            _pick_card(c, p, nm(p["ticker"]))
    else:
        c.y += 20
        c.panel(120, (22, 28, 40))
        c.text(W // 2, "오늘은 조건을 충족한 종목이 없습니다", 32, TXT, True, y=c.y + 42, anchor="mm")
        c.text(W // 2, "억지로 사지 않는 것도 전략입니다", 26, SUB, y=c.y + 84, anchor="mm")
        c.y += 140

    # ── 시장 체크 ──
    checks = m.get("checks") or []
    if checks:
        okn = sum(1 for k in checks if k["ok"])
        c.section("시장 체크", f"{okn}/{len(checks)} 통과")
        h = 36 + 58 * len(checks)
        c.panel(h)
        for i, k in enumerate(checks):
            yy = c.y + 30 + i * 58
            c.mark(PAD + 36, yy, k["ok"])
            c.text(PAD + 86, k["text"], 30, TXT if k["ok"] else (230, 214, 180), y=yy - 4)
        c.y += h

    # ── 감시 후보 ──
    watch = (latest.get("watch") or [])[:5]
    if watch:
        c.section("감시 후보", "조건이 맞으면 추천으로")
        h = 24 + 96 * len(watch)
        c.panel(h)
        for i, w in enumerate(watch):
            yy = c.y + 24 + i * 96
            if i:
                c.d.line((PAD + 36, yy - 4, W - PAD - 36, yy - 4), fill=LINE, width=2)
            c.text(PAD + 36, nm(w["ticker"]), 32, TXT, True, y=yy + 8)
            c.text(PAD + 36, f"{w['ticker']} · {w.get('setup_label', '')} · 기준가 {_krw(w.get('trigger_price'))}", 24, DIM, y=yy + 52)
            d = w.get("dist_pct")
            if d is not None:
                dc = GREEN if abs(d) <= 1.5 else SUB
                c.text(W - PAD - 36, f"{d:+.1f}%", 34, dc, True, y=yy + 22, anchor="ra")
        c.y += h

    # ── 급등 종목 ──
    pumps = (latest.get("pumps") or [])[:4]
    if pumps:
        c.section("최근 7일 급등", "관찰용 · 매수 신호 아님")
        h = 24 + 62 * len(pumps)
        c.panel(h)
        for i, x in enumerate(pumps):
            yy = c.y + 22 + i * 62
            c.text(PAD + 36, nm(x["ticker"]), 28, TXT, True, y=yy)
            c.text(W - PAD - 250, f"+{x['pump_pct']:.0f}%", 28, GREEN, True, y=yy, anchor="ra")
            c.text(W - PAD - 36, f"고점 대비 {x['from_peak_pct']:+.0f}%", 26, RED, y=yy + 2, anchor="ra")
        c.y += h

    # ── 퍼널 ──
    f = latest.get("funnel")
    if f:
        c.y += 34
        steps = [("스캔", f["scanned"]), ("상승추세", f["uptrend"]), ("눌림", f["uptrend_pulled_back"]), ("매수 조건", f["picks"])]
        cw = (W - PAD * 2) / len(steps)
        for i, (k, v) in enumerate(steps):
            x = PAD + cw * i + cw / 2
            c.text(x, str(v), 40, TXT if v else DIM, True, y=c.y, anchor="ma")
            c.text(x, k, 22, DIM, y=c.y + 54, anchor="ma")
            if i:
                c.text(PAD + cw * i, "›", 40, LINE, y=c.y, anchor="ma")
        c.y += 90

    c.y += 10
    c.d.line((PAD, c.y, W - PAD, c.y), fill=LINE, width=2)
    c.y += 24
    c.text(PAD + 4, "투자 판단과 책임은 본인에게 있습니다", 22, DIM)
    return c.done()


if __name__ == "__main__":
    import json
    import sys
    d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "data/latest.json"))
    open(sys.argv[2] if len(sys.argv) > 2 else "card.png", "wb").write(scan_card(d))
