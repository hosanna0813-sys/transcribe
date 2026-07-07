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
import threading
import time
from collections import defaultdict, deque

import yt_dlp
from yt_dlp.utils import download_range_func
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

MAX_VIDEO_SEC = 3 * 3600      # 影片長度上限 3 小時
MAX_CLIP_SEC = 2 * 3600       # 單段截取上限 2 小時
MAX_FILE_BYTES = 100 * 1024 * 1024  # 暫存檔大小上限 100 MB
YT_URL_RE = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com/(watch|shorts|live)|youtu\.be/)", re.I
)

# ---- 公開服務保護:每 IP 速率限制 + 並行下載上限 ----
RATE_WINDOW_SEC = 3600
RATE_LIMITS = {"info": 30, "audio": 10}   # 每 IP 每小時
_rate_lock = threading.Lock()
_rate_hits: dict = defaultdict(deque)     # (bucket, ip) -> deque[timestamp]
_audio_slots = threading.Semaphore(2)     # 同時最多 2 個下載工作


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_check(bucket: str, request: Request) -> None:
    ip = client_ip(request)
    now = time.time()
    key = (bucket, ip)
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] > RATE_WINDOW_SEC:
            hits.popleft()
        if len(hits) >= RATE_LIMITS[bucket]:
            raise HTTPException(429, "使用次數已達上限(防止服務被濫用),請一小時後再試")
        hits.append(now)
        # 順手清掉整批過期的 key,避免記憶體累積
        if len(_rate_hits) > 5000:
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > RATE_WINDOW_SEC]:
                del _rate_hits[k]

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


# YouTube 對機房 IP 常要求登入驗證。兩層突破:
# 1. cookies:Render Secret File 掛載於 /etc/secrets/cookies.txt(最可靠)
# 2. tv 播放器客戶端:被驗證擋下時自動改用 tv client 重試(常可避開)
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
BOT_MSG = "YouTube 要求伺服器驗證身分,暫時無法存取。站長可依 server/README.md 加入 cookies 解決。"


def _bot_blocked(msg: str) -> bool:
    return "Sign in to confirm" in msg or "not a bot" in msg


def _ydl_base(extra: dict | None = None, client: list | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": client}}
    if extra:
        opts.update(extra)
    return opts


def probe(url: str) -> dict:
    last = ""
    for client in (None, ["tv"]):
        opts = _ydl_base({"skip_download": True}, client)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last = str(e)
            if not _bot_blocked(last):
                break
    if _bot_blocked(last):
        raise HTTPException(502, BOT_MSG)
    raise HTTPException(502, f"無法讀取影片資訊:{last[:200]}")


@app.get("/healthz")
def healthz():
    # 一併回報 PO Token 產生器(bgutil,4416 埠)是否運作,方便部署後驗證
    pot = False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=3) as r:
            pot = r.status == 200
    except Exception:
        pot = False
    return {"ok": True, "pot_provider": pot}


@app.get("/diag")
def diag(request: Request, url: str = Query(...)):
    """遠端診斷:回傳 yt-dlp 詳細日誌,用於排查 YouTube 驗證問題(與 /info 共用速率額度)"""
    rate_check("info", request)
    check_url(url)
    lines: list = []

    class Cap:
        def debug(self, m): lines.append(str(m))
        def info(self, m): lines.append(str(m))
        def warning(self, m): lines.append("WARN " + str(m))
        def error(self, m): lines.append("ERR " + str(m))

    for client in (None, ["tv"]):
        opts = _ydl_base({"skip_download": True, "logger": Cap(), "verbose": True}, client)
        opts.pop("quiet", None)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                d = ydl.extract_info(url, download=False)
                return {"ok": True, "client": client, "title": d.get("title"),
                        "log_tail": [l[:300] for l in lines[-50:]]}
        except Exception as e:
            lines.append(f"EXC({client}): {str(e)[:300]}")
    return {"ok": False, "log_tail": [l[:300] for l in lines[-90:]]}


@app.get("/info")
def info(request: Request, url: str = Query(...)):
    rate_check("info", request)
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
    request: Request,
    url: str = Query(...),
    start: float | None = Query(None, ge=0),
    end: float | None = Query(None, gt=0),
):
    rate_check("audio", request)
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

    if not _audio_slots.acquire(blocking=False):
        raise HTTPException(429, "伺服器忙碌中(同時處理數已滿),請稍候一分鐘再試")
    tmpdir = tempfile.mkdtemp(prefix="ytaudio-")
    try:
        resp = _fetch_and_transcode(url, tmpdir, start, end)
    except Exception:
        # 任何失敗都立即清掉暫存,不讓檔案留在伺服器上
        shutil.rmtree(tmpdir, ignore_errors=True)
        _audio_slots.release()
        raise
    _audio_slots.release()
    return resp


def _fetch_and_transcode(url: str, tmpdir: str, start: float | None, end: float | None):
    base_opts = _ydl_base({
        "format": "bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
        "max_filesize": MAX_FILE_BYTES,
    })

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
        last = None
        for client in (None, ["tv"]):
            opts = dict(base_opts)
            if client:
                opts["extractor_args"] = {"youtube": {"player_client": client}}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                last = None
                break
            except Exception as e:
                last = str(e)
                _cleanup_partial()
                if not _bot_blocked(last):
                    break
        if last is not None:
            if _bot_blocked(last):
                raise HTTPException(502, BOT_MSG)
            raise HTTPException(502, f"下載音訊失敗:{last[:200]}")

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
