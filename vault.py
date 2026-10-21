"""계좌 금고 — 공개 저장소에 개인 정보를 암호화해서 보관한다.

구조 (우체통 방식)
  data/vault/pub.json        공개키 — 누구나 '넣기'만 할 수 있음
  data/vault/key.json        비밀번호로 잠근 개인키 — 비밀번호가 있어야 '꺼내기' 가능
  data/vault/snapshot.json   전체 내용(보유 종목·평단·수량·매매기록·감시 상태) 암호문
  data/vault/events/*.json   나중에 추가된 변경 사항 암호문 (봇이 주기적으로 스냅샷에 합침)

암호: RSA-OAEP(SHA-256, 3072비트)로 1회용 AES-256-GCM 키를 감싸고, 내용은 AES-GCM으로 암호화.
개인키(PKCS#8)는 PBKDF2-SHA256(310,000회)로 비밀번호에서 만든 AES-GCM 키로 잠금.
브라우저 WebCrypto(SubtleCrypto)로 똑같이 풀 수 있게 맞춤 (index.html 의 '내 계좌' 탭).
"""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITER = 310_000
KST = timezone(timedelta(hours=9))
_b64 = lambda b: base64.b64encode(b).decode()
_unb64 = lambda s: base64.b64decode(s)
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def _dir(base):
    return os.path.join(base, "vault")


def _kek(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER).derive(password.encode("utf-8"))


def init_keys(base: str, password: str) -> None:
    """키 한 쌍을 만들고 개인키를 비밀번호로 잠가 저장 (이미 있으면 아무것도 안 함)."""
    d = _dir(base)
    if os.path.exists(os.path.join(d, "pub.json")):
        return
    os.makedirs(os.path.join(d, "events"), exist_ok=True)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pkcs8 = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kek(password, salt)).encrypt(iv, pkcs8, None)
    json.dump({"v": 1, "alg": "RSA-OAEP-256", "spki": _b64(spki)}, open(os.path.join(d, "pub.json"), "w"))
    json.dump({"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)},
              open(os.path.join(d, "key.json"), "w"))


def seal(base: str, obj) -> dict:
    """공개키로 잠금 — 비밀번호 없이도 가능."""
    pub = serialization.load_der_public_key(_unb64(json.load(open(os.path.join(_dir(base), "pub.json")))["spki"]))
    k, iv = AESGCM.generate_key(256), os.urandom(12)
    ct = AESGCM(k).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), None)
    return {"v": 1, "alg": "RSA-OAEP-256+A256GCM", "ek": _b64(pub.encrypt(k, _OAEP)), "iv": _b64(iv), "ct": _b64(ct)}


def _private_key(base: str, password: str):
    kj = json.load(open(os.path.join(_dir(base), "key.json")))
    pkcs8 = AESGCM(_kek(password, _unb64(kj["salt"]))).decrypt(_unb64(kj["iv"]), _unb64(kj["ct"]), None)
    return serialization.load_der_private_key(pkcs8, password=None)


def unseal(priv, box: dict):
    k = priv.decrypt(_unb64(box["ek"]), _OAEP)
    return json.loads(AESGCM(k).decrypt(_unb64(box["iv"]), _unb64(box["ct"]), None))


def add_event(base: str, ev: dict) -> str:
    """변경 사항 하나를 잠가서 events/ 에 추가 (Claude·봇 누구나, 비밀번호 불필요)."""
    d = os.path.join(_dir(base), "events")
    os.makedirs(d, exist_ok=True)
    ev = {**ev, "at": ev.get("at") or datetime.now(KST).isoformat(timespec="seconds")}
    name = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}.json"
    json.dump(seal(base, ev), open(os.path.join(d, name), "w"))
    _write_index(base)
    return name


def _write_index(base: str) -> None:
    """브라우저는 폴더 목록을 못 보므로 이벤트 파일 이름 목록을 따로 둔다 (이름엔 시각만 들어 있음)."""
    d = os.path.join(_dir(base), "events")
    os.makedirs(d, exist_ok=True)
    files = sorted(f for f in os.listdir(d) if f.endswith(".json") and f != "index.json")
    json.dump({"files": files}, open(os.path.join(d, "index.json"), "w"))


EMPTY = {"positions": [], "trades": [], "state": {}, "cash": None, "base": None, "notes": {}}


def apply_event(snap: dict, ev: dict) -> dict:
    op = ev.get("op")
    P = {p["ticker"]: p for p in snap.setdefault("positions", [])}
    if op == "set":                       # 통째로 교체할 키들
        for k, v in (ev.get("data") or {}).items():
            snap[k] = v
    elif op == "upsert_position":
        p = ev["position"]
        P[p["ticker"]] = {**P.get(p["ticker"], {}), **p}
        snap["positions"] = list(P.values())
    elif op == "remove_position":
        snap["positions"] = [p for t, p in P.items() if t != ev["ticker"]]
        snap.setdefault("state", {}).pop(ev["ticker"], None)
    elif op == "add_trade":
        snap.setdefault("trades", []).append(ev["trade"])
    return snap


def load(base: str, password: str):
    """스냅샷 + 쌓인 이벤트를 합친 현재 내용과, 합친 이벤트 파일 목록."""
    priv = _private_key(base, password)
    p = os.path.join(_dir(base), "snapshot.json")
    snap = unseal(priv, json.load(open(p))) if os.path.exists(p) else json.loads(json.dumps(EMPTY))
    ed = os.path.join(_dir(base), "events")
    files = sorted(f for f in os.listdir(ed) if f.endswith(".json") and f != "index.json") if os.path.isdir(ed) else []
    for f in files:
        snap = apply_event(snap, unseal(priv, json.load(open(os.path.join(ed, f)))))
    return snap, files


def save(base: str, snap: dict, merged_events=()) -> None:
    """스냅샷을 다시 잠가 저장하고, 합친 이벤트 파일은 지운다."""
    snap["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    json.dump(seal(base, snap), open(os.path.join(_dir(base), "snapshot.json"), "w"))
    for f in merged_events:
        try:
            os.remove(os.path.join(_dir(base), "events", f))
        except FileNotFoundError:
            pass
    _write_index(base)


def ready(base: str) -> bool:
    return os.path.exists(os.path.join(_dir(base), "pub.json"))
