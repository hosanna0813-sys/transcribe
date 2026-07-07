"""
YouTube 音訊擷取服務(供「語音轉逐字稿」前端使用)

只做一件事:把 YouTube 影片(或指定的起訖片段)抓下來、壓成最小的
16kHz 單聲道 32kbps AAC 音訊回傳給瀏覽器,瀏覽器再用使用者自己的
OpenAI API Key 走 Whisper 轉錄。

零保存原則:暫存檔寫入系統暫存目錄,回應送出後立即刪除;
伺服器不保存任何音訊、不經手任何 API Key。
"""

import os
import re
import glob
import shutil
import subprocess
import tempfile

import yt_dlp
from yt_dlp.utils import download_range_func
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

MAX_VIDEO_SEC = 3 * 3600      # 影片長度上限 3 小時
MAX_CLIP_SEC = 2 * 3600       # 單段截取上限 2 小時
MAX_FILE_BYTES = 100 * 1024 * 1024  # 暫存檔大小上限 100 MB
YT_URL_RE = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com/(watch|shorts|live)|youtu\.be/)", re.I
)

app = FastAPI(title="transcribe-yt", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hosanna0813-sys.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "null",  # 以 file:// 直接開啟 index.html 時的 origin
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def check_url(url: str) -> None:
    if not YT_URL_RE.match(url or ""):
        raise HTTPException(400, "請提供有效的 YouTube 影片網址")


def probe(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(502, f"無法讀取影片資訊:{str(e)[:200]}")
    return info


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/info")
def info(url: str = Query(...)):
    check_url(url)
    d = probe(url)
    duration = int(d.get("duration") or 0)
    return {
        "title": d.get("title") or "YouTube 影片",
        "duration": duration,
        "too_long": duration > MAX_VIDEO_SEC,
        "max_video_sec": MAX_VIDEO_SEC,
    }


@app.get("/audio")
def audio(
    url: str = Query(...),
    start: float | None = Query(None, ge=0),
    end: float | None = Query(None, gt=0),
):
    check_url(url)
    if start is not None and end is not None and end <= start:
        raise HTTPException(400, "終點必須大於起點")

    meta = probe(url)
    duration = int(meta.get("duration") or 0)
    clip_len = (end if end is not None else duration or MAX_VIDEO_SEC) - (start or 0)
    if start is None and end is None:
        if duration > MAX_VIDEO_SEC:
            raise HTTPException(400, "影片超過 3 小時,請改用起訖時間截取需要的片段")
    elif clip_len > MAX_CLIP_SEC:
        raise HTTPException(400, "截取片段超過 2 小時上限,請縮短範圍")

    tmpdir = tempfile.mkdtemp(prefix="ytaudio-")
    try:
        return _fetch_and_transcode(url, tmpdir, start, end)
    except Exception:
        # 任何失敗都立即清掉暫存,不讓檔案留在伺服器上
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _fetch_and_transcode(url: str, tmpdir: str, start: float | None, end: float | None):
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
        "max_filesize": MAX_FILE_BYTES,
        "socket_timeout": 30,
    }

    def _cleanup_partial():
        for p in glob.glob(os.path.join(tmpdir, "src.*")):
            try:
                os.remove(p)
            except OSError:
                pass

    want_clip = start is not None or end is not None
    already_clipped = False
    if want_clip:
        # 先試片段下載(只抓需要的範圍,最省流量);此模式由 ffmpeg 直連
        # YouTube 媒體網址,部分機房 IP 會被 403 拒絕,失敗就改走完整下載
        sec_opts = dict(base_opts)
        sec_opts["download_ranges"] = download_range_func(
            None, [(start or 0, end if end is not None else float("inf"))]
        )
        try:
            with yt_dlp.YoutubeDL(sec_opts) as ydl:
                ydl.download([url])
            already_clipped = True
        except Exception:
            _cleanup_partial()

    if not already_clipped:
        try:
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            raise HTTPException(502, f"下載音訊失敗:{str(e)[:200]}")

    srcs = glob.glob(os.path.join(tmpdir, "src.*"))
    if not srcs:
        raise HTTPException(502, "下載音訊失敗:音訊可能超過 100 MB 上限,請用起訖時間縮短範圍")
    src = srcs[0]
    if os.path.getsize(src) > MAX_FILE_BYTES:
        raise HTTPException(413, "音訊超過 100 MB 上限,請用起訖時間縮短範圍")

    # 轉成 16kHz 單聲道 32kbps AAC;若片段下載失敗改抓了全片,就在這裡用 -ss/-t 截取
    out = os.path.join(tmpdir, "audio.m4a")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if want_clip and not already_clipped:
        if start:
            cmd += ["-ss", str(start)]
        if end is not None:
            cmd += ["-t", str(end - (start or 0))]
    cmd += ["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
            "-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"音訊轉檔失敗:{(e.stderr or b'').decode(errors='ignore')[:200]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "音訊轉檔逾時")
    os.remove(src)  # 轉檔完成即刪來源檔,同一時間只留最小輸出檔

    if os.path.getsize(out) > MAX_FILE_BYTES:
        raise HTTPException(413, "音訊超過 100 MB 上限,請用起訖時間縮短範圍")

    # 回應送出後立即刪除整個暫存目錄(附掛在 Response 上才會確實執行)
    return FileResponse(
        out,
        media_type="audio/mp4",
        filename="youtube_audio.m4a",
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )
