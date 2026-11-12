"""호가·체결 기록 (GitHub 작업에서 실행) — 공개 시세만, 키·계좌·주문 없음.

server/coinbot.py 의 검증된 기록기(MMWS·MiniWS)를 그대로 쓴다.
  · 기록 묶음(run)은 data/research/mmrec/run.json 에 고정: 시작 시각, 끝 시각(시작+3일), 종목(시작 시점 24시간 거래대금 상위 10개)
  · 작업 하나는 RECORD_MIN 분 기록하고 끝. 3시간마다 새 작업이 시작되므로 앞뒤가 겹쳐 빈틈이 없다(겹친 체결은 고유 번호로 거름)
  · 파일 이름 앞에 작업 번호를 붙여 겹치는 작업끼리 덮어쓰지 않게 한다: <작업번호>_<코인>_<날짜>T<시>.csv.gz
  · 상태는 data/research/mmrec/status_<작업번호>.json
"""
import json, os, sys, tempfile, time
sys.path.insert(0, "server")
import coinbot as c

RUN_FILE = "data/research/mmrec/run.json"
RECORD_MIN = float(os.environ.get("RECORD_MIN", "220"))
DAYS = float(os.environ.get("RUN_DAYS", "3"))
JOB = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
os.makedirs(os.path.dirname(RUN_FILE), exist_ok=True)


def load_run():
    try:
        return json.load(open(RUN_FILE))
    except (OSError, ValueError):
        return None


run = load_run()
now = time.time()
if run is None:
    rank = c.MMWS.rank_markets(10)
    if len(rank) < 3:
        sys.exit("종목 선정 실패")
    run = {"run_id": "gh1", "start": now, "end": now + DAYS * 86400, "start_kst": c.kst(now).isoformat(timespec="seconds"),
           "rule": "기록 시작 시점 24시간 거래대금 상위 10개 KRW 마켓 (끝까지 고정)",
           "markets": [m for m, _ in rank], "rank_at_start": [[m, round(v / 1e8, 1)] for m, v in rank]}
    json.dump(run, open(RUN_FILE, "w"), ensure_ascii=False, indent=1)
    print("새 기록 묶음:", run["markets"])
if "--init" in sys.argv:                      # 종목만 정하고 끝 (작업 시작 직후 바로 저장소에 올려 다음 작업이 같은 종목을 쓰게 함)
    sys.exit(0)
if now >= run["end"]:
    print("기록 기간이 끝났어요:", run["start_kst"], "부터 3일")
    sys.exit(0)

minutes = min(RECORD_MIN, (run["end"] - now) / 60)
uploads, msgs = [], []


def uploader(path, name):
    code = c.gh_release_upload(path, f"{JOB}_{name}")
    uploads.append([f"{JOB}_{name}", code, os.path.getsize(path)])
    return code


def status_put(path, raw, message):
    open(f"data/research/mmrec/status_{JOB}.json", "wb").write(raw)
    return True


rec = c.MMWS(base=tempfile.mkdtemp(), days=minutes / 1440, markets=run["markets"], uploader=uploader,
             status_put=status_put, sender=lambda s: msgs.append(s.replace("<b>", "").replace("</b>", "")))
rec.run()
st = rec.status()
st.update(job=JOB, run=run["run_id"], uploads=uploads, messages=msgs, minutes=round(minutes, 1))
json.dump(st, open(f"data/research/mmrec/status_{JOB}.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps({k: st[k] for k in ("counts_trade_book", "latency_ms", "session", "uploaded_files", "upload_fail", "pending_files")}, ensure_ascii=False, indent=1))
bad = [u for u in uploads if u[1] != 201]
if bad or st["pending_files"]:
    sys.exit(f"업로드 실패 {len(bad)}개, 남은 파일 {st['pending_files']}개")
