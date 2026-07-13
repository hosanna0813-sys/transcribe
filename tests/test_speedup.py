"""長音檔提速:校正並行(保序/usage/進度)、單趟 ffmpeg 切段、CHUNK_WORKERS 設定。
全部 mock,不需網路/Postgres。"""
import threading
import time

import httpx
import pytest


# ---------- _gpt_correct 並行:亂序完成仍保序、usage 正確、進度回報 ----------

def _three_chunk_text():
    # _chunk_text(max_len=6000):三段各 4000 字 → 恰好三塊
    return "\n\n".join(f"C{i} " + "x" * 4000 for i in range(3))


class _Resp:
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_gpt_correct_parallel_keeps_order(m, monkeypatch):
    seen = []
    lock = threading.Lock()

    def fake_post(url, headers=None, json=None, timeout=None):
        chunk = json["messages"][1]["content"]
        tag = chunk[:2]                     # "C0" / "C1" / "C2"
        time.sleep({"C0": 0.2, "C1": 0.05, "C2": 0.0}[tag])   # 讓完成順序反過來
        with lock:
            seen.append(tag)
        return _Resp(f"FIXED-{tag}")

    monkeypatch.setattr(httpx, "post", fake_post)
    usage = {}
    prog = []
    out = m._gpt_correct(_three_chunk_text(), "sk-test", usage=usage,
                         on_progress=lambda d, t: prog.append((d, t)))
    # 完成順序亂(C2 最先),輸出仍照原始順序拼回
    assert seen != ["C0", "C1", "C2"]
    assert out == "FIXED-C0\n\nFIXED-C1\n\nFIXED-C2"
    # usage 由主執行緒統一累加,總量正確
    assert usage == {"prompt_tokens": 30, "completion_tokens": 15, "calls": 3}
    # 進度回報 n 次,最後一筆 (3, 3)
    assert len(prog) == 3 and prog[-1] == (3, 3)


def test_gpt_correct_chunk_failure_raises(m, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        chunk = json["messages"][1]["content"]
        if chunk.startswith("C1"):
            r = _Resp("")
            r.status_code = 500
            r.text = "boom"
            return r
        return _Resp("ok")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError):
        m._gpt_correct(_three_chunk_text(), "sk-test")


# ---------- _transcode_and_segment:單一 ffmpeg;copy vs 編碼 ----------

def _capture_run(monkeypatch, m):
    calls = []

    def fake_run(cmd, check=True, timeout=None, capture_output=True):
        calls.append(cmd)

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    return calls


def test_segment_normalized_uses_copy_single_pass(m, monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch, m)
    m._transcode_and_segment("/tmp/src", str(tmp_path), normalized=True)
    assert len(calls) == 1                       # 只有一趟 ffmpeg
    cmd = calls[0]
    assert "copy" in cmd and "segment" in cmd    # 純封裝切段
    assert "-ar" not in cmd                      # 不重新編碼


def test_segment_upload_encodes_and_segments_in_one_pass(m, monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch, m)
    m._transcode_and_segment("/tmp/src", str(tmp_path))
    assert len(calls) == 1                       # 編碼與切段合併成一趟
    cmd = calls[0]
    assert "-ar" in cmd and "16000" in cmd and "segment" in cmd
    assert "copy" not in cmd


# ---------- CHUNK_WORKERS ----------

def test_chunk_workers_default(m):
    assert isinstance(m.CHUNK_WORKERS, int) and m.CHUNK_WORKERS == 6
