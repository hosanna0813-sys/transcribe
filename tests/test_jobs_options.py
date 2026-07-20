"""/api/jobs 轉錄選項:p_options 送出內容、phase9 未套用時的舊簽名 fallback。
全部 mock,不需 Postgres/網路。"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture()
def jobenv(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_jwt", lambda a: "00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(m, "_user_rate_check", lambda uid: None)
    monkeypatch.setattr(m, "rate_check", lambda b, r: None)
    monkeypatch.setattr(m, "_content_length_precheck", lambda r: None)
    monkeypatch.setattr(m, "_relay_info", lambda url: {"duration": 600, "too_long": False})
    return m


def _post(m, data):
    c = TestClient(m.app)
    return c.post("/api/jobs", data=data, headers={"Authorization": "Bearer x"})


def test_jobs_sends_options(jobenv, monkeypatch):
    m = jobenv
    calls = []

    def fake_rpc(fn, payload):
        calls.append((fn, payload))
        return [{"ok": True, "usage_id": "11111111-1111-1111-1111-111111111111", "remaining": 1000}]

    monkeypatch.setattr(m, "_sb_rpc", fake_rpc)
    r = _post(m, {"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "correct": "true",
                  "timestamps": "false", "remove_fillers": "true", "speakers": "true"})
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    assert calls[0][1]["p_options"] == {"timestamps": False, "remove_fillers": True, "speakers": True}
    assert calls[0][1]["p_correct"] is True


def test_jobs_options_default(jobenv, monkeypatch):
    """未帶選項欄位:時間戳預設開(維持原長音檔行為)"""
    m = jobenv
    calls = []

    def fake_rpc(fn, payload):
        calls.append(payload)
        return [{"ok": True, "usage_id": "11111111-1111-1111-1111-111111111111", "remaining": 1000}]

    monkeypatch.setattr(m, "_sb_rpc", fake_rpc)
    r = _post(m, {"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200, r.text
    assert calls[0]["p_options"] == {"timestamps": True, "remove_fillers": False, "speakers": False}


def test_jobs_fallback_without_options_on_pgrst202(jobenv, monkeypatch):
    """phase9 未套用(PGRST202)→ 自動改用不帶 p_options 的舊簽名重試,任務照常建立"""
    m = jobenv
    calls = []

    def fake_rpc(fn, payload):
        calls.append(payload)
        if "p_options" in payload:
            raise HTTPException(502, 'RPC 失敗:{"code":"PGRST202","message":"function p_options not found"}')
        return [{"ok": True, "usage_id": "11111111-1111-1111-1111-111111111111", "remaining": 1000}]

    monkeypatch.setattr(m, "_sb_rpc", fake_rpc)
    r = _post(m, {"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "timestamps": "false"})
    assert r.status_code == 200, r.text
    assert len(calls) == 2
    assert "p_options" in calls[0] and "p_options" not in calls[1]


def test_jobs_phase8_missing_still_503(jobenv, monkeypatch):
    """連 phase8 都沒套用(兩次都 PGRST202)→ 維持明確的 503 升級中訊息"""
    m = jobenv

    def fake_rpc(fn, payload):
        raise HTTPException(502, 'RPC 失敗:{"code":"PGRST202","message":"enqueue_transcription not found"}')

    monkeypatch.setattr(m, "_sb_rpc", fake_rpc)
    r = _post(m, {"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 503
    assert "升級中" in r.json()["detail"]
