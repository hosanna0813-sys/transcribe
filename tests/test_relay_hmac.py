"""家用中繼 HMAC 簽章:正確簽章、錯誤簽章、過期 timestamp、nonce 重放、fail closed"""
import time

import pytest
from fastapi import HTTPException


SECRET = "test-relay-secret"


class FakeURL:
    path = "/audio"


class FakeReq:
    method = "GET"
    url = FakeURL()

    def __init__(self, headers=None, query=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = query or {}


def signed_headers(m, query, ts=None, nonce="n0nce123"):
    ts = str(int(time.time())) if ts is None else ts
    sig = m._relay_sign(SECRET, ts, nonce, "GET", "/audio", query)
    return {"X-Relay-Timestamp": ts, "X-Relay-Nonce": nonce, "X-Relay-Signature": sig}


@pytest.fixture(autouse=True)
def relay_env(monkeypatch):
    monkeypatch.setenv("RELAY_REQUIRE_AUTH", "1")
    monkeypatch.setenv("RELAY_SHARED_SECRET", SECRET)


def test_valid_signature_passes(m):
    q = {"url": "https://youtu.be/abc", "start": "5"}
    m.relay_guard(FakeReq(signed_headers(m, q), q))   # 不應丟例外


def test_missing_headers_403(m):
    with pytest.raises(HTTPException) as e:
        m.relay_guard(FakeReq({}, {"url": "x"}))
    assert e.value.status_code == 403


def test_wrong_signature_403(m):
    q = {"url": "x"}
    h = signed_headers(m, q)
    h["X-Relay-Signature"] = "0" * 64
    with pytest.raises(HTTPException) as e:
        m.relay_guard(FakeReq(h, q))
    assert e.value.status_code == 403


def test_tampered_query_403(m):
    q = {"url": "https://youtu.be/abc"}
    h = signed_headers(m, q)
    with pytest.raises(HTTPException):
        m.relay_guard(FakeReq(h, {"url": "https://youtu.be/EVIL"}))


def test_expired_timestamp_403(m):
    q = {"url": "x"}
    old = str(int(time.time()) - 120)
    with pytest.raises(HTTPException) as e:
        m.relay_guard(FakeReq(signed_headers(m, q, ts=old), q))
    assert e.value.status_code == 403


def test_nonce_replay_rejected(m):
    q = {"url": "x"}
    h = signed_headers(m, q, nonce="replay-me")
    m.relay_guard(FakeReq(h, q))            # 第一次通過
    with pytest.raises(HTTPException) as e:  # 同 nonce 重放必須被拒
        m.relay_guard(FakeReq(h, q))
    assert e.value.status_code == 403


def test_missing_secret_fail_closed(m, monkeypatch):
    monkeypatch.delenv("RELAY_SHARED_SECRET")
    q = {"url": "x"}
    with pytest.raises(HTTPException) as e:
        m.relay_guard(FakeReq(signed_headers(m, q), q))
    assert e.value.status_code == 403


def test_auth_not_required_passes(m, monkeypatch):
    monkeypatch.delenv("RELAY_REQUIRE_AUTH")
    m.relay_guard(FakeReq({}, {}))   # 公開模式(Render)不驗證


def test_sign_headers_roundtrip(m, monkeypatch):
    """後端 _relay_sign_headers 產生的簽章必須能被 relay_guard 驗過(值含中文/特殊字)"""
    q = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "start": "1.5", "end": "600"}
    h = m._relay_sign_headers("GET", "/audio", q)
    m.relay_guard(FakeReq(h, q))
