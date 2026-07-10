"""家用中繼 HMAC 簽章驗證(防 ngrok 網址外流被盜用)。

家用中繼(server/home)設 RELAY_REQUIRE_AUTH=1 + RELAY_SHARED_SECRET 後,
/info /caption /audio /diag 需通過簽章(timestamp±60s、nonce 防重放、compare_digest,
失敗一律 403)。Render 後端設同一 RELAY_SHARED_SECRET,對中繼請求自動簽章。
環境變數於呼叫時讀取,便於測試。
"""
import hmac
import os
import secrets
import threading
import time

from fastapi import HTTPException, Request

RELAY_SIG_SKEW_SEC = 60
_relay_nonces: dict = {}          # nonce -> expiry_ts
_relay_nonce_lock = threading.Lock()


def _relay_secret() -> str:
    return os.environ.get("RELAY_SHARED_SECRET", "")


def _relay_auth_required() -> bool:
    return os.environ.get("RELAY_REQUIRE_AUTH", "").strip() in ("1", "true", "yes")


def _relay_sign(secret: str, ts: str, nonce: str, method: str, path: str, query: dict) -> str:
    cq = "&".join(f"{k}={query[k]}" for k in sorted(query))
    msg = f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{cq}"
    return hmac.new(secret.encode(), msg.encode(), "sha256").hexdigest()


def _relay_sign_headers(method: str, path: str, query: dict) -> dict:
    """對外呼叫家用中繼時附上簽章(未設密鑰則不附,由中繼端決定是否拒絕)"""
    secret = _relay_secret()
    if not secret:
        return {}
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    return {"X-Relay-Timestamp": ts, "X-Relay-Nonce": nonce,
            "X-Relay-Signature": _relay_sign(secret, ts, nonce, method, path, query)}


def relay_guard(request: Request) -> None:
    """RELAY_REQUIRE_AUTH=1 時強制驗證簽章;失敗一律 403、不洩漏細節"""
    if not _relay_auth_required():
        return
    secret = _relay_secret()
    if not secret:
        raise HTTPException(403, "服務未開放")   # fail closed:要求驗證但沒設密鑰
    ts = request.headers.get("x-relay-timestamp", "")
    nonce = request.headers.get("x-relay-nonce", "")
    sig = request.headers.get("x-relay-signature", "")
    if not (ts and nonce and sig):
        raise HTTPException(403, "服務未開放")
    try:
        if abs(time.time() - int(ts)) > RELAY_SIG_SKEW_SEC:
            raise ValueError
    except ValueError:
        raise HTTPException(403, "服務未開放")
    query = {k: v for k, v in request.query_params.items()}
    expect = _relay_sign(secret, ts, nonce, request.method, request.url.path, query)
    if not hmac.compare_digest(expect, sig):
        raise HTTPException(403, "服務未開放")
    now = time.time()
    with _relay_nonce_lock:
        for k in [k for k, exp in _relay_nonces.items() if exp < now]:
            del _relay_nonces[k]
        if nonce in _relay_nonces:
            raise HTTPException(403, "服務未開放")   # nonce 重放
        _relay_nonces[nonce] = now + RELAY_SIG_SKEW_SEC * 2
