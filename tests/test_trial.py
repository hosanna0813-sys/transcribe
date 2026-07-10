"""免費試用:扣除、失敗退款、偽造 IP 無法繞過、裝置 ID、全站上限"""
import shutil
import subprocess
import tempfile
import os

import pytest

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def wav():
    if not FFMPEG:
        pytest.skip("需要 ffmpeg")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "a.wav")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=5", "-ar", "16000", "-ac", "1", path],
                   check=True)
    yield path
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def mock_ai(m, monkeypatch):
    monkeypatch.setattr(m, "_whisper_transcribe", lambda p, k: "測試逐字稿")
    monkeypatch.setattr(m, "_gpt_correct",
                        lambda t, k, remove_fillers=False, speakers=False: t + "!")
    # 這組測試針對端點+額度邏輯,走記憶體後備路徑(資料庫路徑由 test_sql.py 覆蓋)
    monkeypatch.setattr(m, "_trial_db_enabled", lambda: False)


def post(client, wav, ip, dev=None):
    data = {"correct": "false"}
    if dev:
        data["device_id"] = dev
    with open(wav, "rb") as f:
        return client.post("/api/trial", files={"file": ("a.wav", f, "audio/wav")},
                           data=data, headers={"X-Forwarded-For": ip})


def test_deduct_and_refund(client, m, wav, monkeypatch):
    # 成功:扣 5 秒
    r = post(client, wav, "10.1.0.1")
    assert r.status_code == 200
    assert m._trial_remaining(["10.1.0.1"]) == 595
    # Whisper 失敗:退回
    monkeypatch.setattr(m, "_whisper_transcribe",
                        lambda p, k: (_ for _ in ()).throw(RuntimeError("down")))
    r = post(client, wav, "10.1.0.2")
    assert r.status_code == 502
    assert m._trial_remaining(["10.1.0.2"]) == 600   # 未消耗


def test_forged_xff_cannot_bypass(client, m, wav):
    # 同一個真實連線,不管左邊塞什麼假 IP,計量都落在最右可信值
    for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        r = post(client, wav, f"{fake}, 10.2.0.9")
        assert r.status_code == 200
    assert m._trial_remaining(["10.2.0.9"]) == 600 - 15   # 三次都算同一 IP


def test_device_id_limits(client, m, wav):
    dev = "123e4567-e89b-12d3-a456-426614174000"
    r = post(client, wav, "10.3.0.1", dev)
    assert r.status_code == 200
    # 換 IP、同裝置:裝置鍵仍累計
    assert m._trial_remaining(["dev:" + dev]) == 595
    # 不合法裝置 ID 不成為身分(不炸、不多算)
    r = post(client, wav, "10.3.0.2", "<script>alert(1)</script>")
    assert r.status_code == 200


def test_global_daily_cap(client, m, wav, monkeypatch):
    monkeypatch.setenv("TRIAL_DAILY_TOTAL_MINUTES", "1")   # 全站每日 60 秒
    hit = False
    for i in range(20):
        r = post(client, wav, f"10.4.{i}.1")
        if r.status_code == 429 and "總量" in r.json()["detail"]:
            hit = True
            break
    assert hit


def test_status_endpoint(client):
    r = client.get("/api/trial/status", headers={"X-Forwarded-For": "10.5.0.1"})
    d = r.json()
    assert d["enabled"] is True and d["trial_minutes"] == 10
