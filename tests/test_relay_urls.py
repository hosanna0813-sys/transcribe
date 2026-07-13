"""多家用中繼:HOME_RELAY_URL 解析 + _relay_info 依序嘗試/自動切換(failover)。
純函式與 mock urlopen,不需 Postgres/網路。"""
import io
import itertools
import json
import urllib.error

import pytest
from fastapi import HTTPException


# ---------- _relay_urls 解析 ----------

def test_relay_urls_empty(m, monkeypatch):
    monkeypatch.delenv("HOME_RELAY_URL", raising=False)
    assert m._relay_urls() == []


def test_relay_urls_single_strips_slash(m, monkeypatch):
    monkeypatch.setenv("HOME_RELAY_URL", "https://a.ngrok-free.app/")
    assert m._relay_urls() == ["https://a.ngrok-free.app"]


def test_relay_urls_multi_trims_and_splits(m, monkeypatch):
    monkeypatch.setenv("HOME_RELAY_URL", " https://a.app/ , https://b.app , ")
    # 兩台時起點會輪替,但集合固定為這兩台
    assert sorted(m._relay_urls()) == ["https://a.app", "https://b.app"]


def test_relay_urls_rotates_start(m, monkeypatch):
    monkeypatch.setenv("HOME_RELAY_URL", "https://a.app,https://b.app")
    monkeypatch.setattr(m, "_relay_rr", itertools.count())
    first = m._relay_urls()    # k=0 → [a, b]
    second = m._relay_urls()   # k=1 → [b, a]
    assert first == ["https://a.app", "https://b.app"]
    assert second == ["https://b.app", "https://a.app"]


# ---------- _relay_info failover ----------

def _ok_resp(payload):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()
    return _R()


def test_relay_info_fails_over_to_second(m, monkeypatch):
    """第一台連線失敗 → 自動改用第二台並回其結果。"""
    monkeypatch.setenv("HOME_RELAY_URL", "https://a.app,https://b.app")
    monkeypatch.setattr(m, "_relay_rr", itertools.count())   # 起點固定 [a, b]
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if req.full_url.startswith("https://a.app"):
            raise urllib.error.URLError("home computer off")
        return _ok_resp({"duration": 123, "too_long": False})

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    got = m._relay_info("https://youtu.be/x")
    assert got["duration"] == 123
    assert len(calls) == 2                       # a 失敗、b 成功
    assert calls[0].startswith("https://a.app")
    assert calls[1].startswith("https://b.app")


def test_relay_info_400_short_circuits(m, monkeypatch):
    """第一台回 400(影片本身問題)→ 立即拋出,不再試第二台。"""
    monkeypatch.setenv("HOME_RELAY_URL", "https://a.app,https://b.app")
    monkeypatch.setattr(m, "_relay_rr", itertools.count())
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad", {}, io.BytesIO(b'{"detail":"\\u5f71\\u7247\\u4e0d\\u5b58\\u5728"}'))

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HTTPException) as ei:
        m._relay_info("https://youtu.be/x")
    assert ei.value.status_code == 400
    assert len(calls) == 1                        # 只打第一台


def test_relay_info_no_relay_configured(m, monkeypatch):
    monkeypatch.delenv("HOME_RELAY_URL", raising=False)
    with pytest.raises(HTTPException) as ei:
        m._relay_info("https://youtu.be/x")
    assert ei.value.status_code == 503
