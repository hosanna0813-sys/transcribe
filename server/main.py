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
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
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

# 同請求去重:瀏覽器對久跑的 /audio 會在 300 秒斷線重試(Chrome 硬限制),
# 重試必須「接上」進行中的下載,而不是再開一份重複下載互搶頻寬。
# 暫存目錄用引用計數清理:所有回應送完即刪,維持零保存原則。
_jobs_lock = threading.Lock()
_jobs: dict = {}                          # (url, start, end) -> _AudioJob


class _AudioJob:
    def __init__(self):
        self.done = threading.Event()
        self.path: str | None = None
        self.error: HTTPException | None = None
        self.tmpdir = tempfile.mkdtemp(prefix="ytaudio-")
        self.refs = 0


def _job_release(key, job: "_AudioJob"):
    with _jobs_lock:
        job.refs -= 1
        last = job.refs <= 0
        if last:
            _jobs.pop(key, None)
    if last:
        shutil.rmtree(job.tmpdir, ignore_errors=True)


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
    allow_methods=["GET", "POST"],
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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        # yt-dlp 解 YouTube JS 挑戰需要 JS 執行環境;預設只找 deno,
        # 這裡明確啟用容器內的 Node(求解腳本由 yt-dlp-ejs 套件內建提供)
        "js_runtimes": {"node": {}},
    }
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
        # 只列人工上傳字幕,自動字幕品質不穩定,不適合當校正參考
        "captions": sorted((d.get("subtitles") or {}).keys()),
    }


CAPTION_TEXT_LIMIT = 20000  # 字幕僅供校正參考用,截斷避免推高校正 token 費用


def _vtt_to_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    lines = []
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT":
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        if re.match(r"^(Kind|Language):", line, re.I):  # WEBVTT 標頭中繼資料列
            continue
        line = re.sub(r"<[^>]+>", "", line)  # 去除 <c>、<i> 之類的字幕標記
        if not line or line == last:
            continue
        lines.append(line)
        last = line
    return " ".join(lines)[:CAPTION_TEXT_LIMIT]


@app.get("/caption")
def caption(request: Request, url: str = Query(...), lang: str = Query(...)):
    rate_check("info", request)  # 輕量文字下載,沿用 info 額度
    check_url(url)
    tmpdir = tempfile.mkdtemp(prefix="ytcap-")
    try:
        opts = _ydl_base({
            "skip_download": True,
            "writesubtitles": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmpdir, "cap.%(ext)s"),
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            raise HTTPException(502, f"字幕下載失敗:{str(e)[:200]}")
        vtts = glob.glob(os.path.join(tmpdir, "cap*.vtt"))
        if not vtts:
            raise HTTPException(404, "找不到該語言的字幕")
        return {"text": _vtt_to_text(vtts[0])}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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

    # 同請求去重:第一個請求是「擁有者」負責下載;瀏覽器 300 秒斷線後的
    # 自動重試會以「等待者」身分接上同一份工作,不再重複下載互搶頻寬
    key = (url, start, end)
    with _jobs_lock:
        job = _jobs.get(key)
        owner = job is None
        if owner:
            job = _AudioJob()
            _jobs[key] = job
        job.refs += 1

    if owner:
        if not _audio_slots.acquire(blocking=False):
            job.error = HTTPException(429, "伺服器忙碌中(同時處理數已滿),請稍候一分鐘再試")
            job.done.set()
        else:
            try:
                job.path = _fetch_and_transcode(url, job.tmpdir, start, end)
            except HTTPException as e:
                job.error = e
            except Exception as e:
                job.error = HTTPException(502, f"下載音訊失敗:{str(e)[:200]}")
            finally:
                _audio_slots.release()
                job.done.set()
    else:
        if not job.done.wait(timeout=900):
            _job_release(key, job)
            raise HTTPException(504, "音訊處理逾時,請稍後再試")

    if job.error is not None:
        err = job.error
        _job_release(key, job)
        raise err
    # 回應送出後由引用計數清理:最後一個回應送完即刪整個暫存目錄
    return FileResponse(
        job.path,
        media_type="audio/mp4",
        filename="youtube_audio.m4a",
        background=BackgroundTask(_job_release, key, job),
    )


def _fetch_and_transcode(url: str, tmpdir: str, start: float | None, end: float | None):
    base_opts = _ydl_base({
        "format": "bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
        "max_filesize": MAX_FILE_BYTES,
        # 直播存檔等分段格式常被 YouTube 限速,開 8 線並行下載分段
        "concurrent_fragment_downloads": 8,
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
        # YouTube 媒體網址,部分機房 IP 會被 403 拒絕,失敗就改走完整下載。
        # 只對「漸進式 https 格式」嘗試:直播存檔只有分段 DASH 格式,
        # ffmpeg 直連必被拒(exit 183),限定格式讓它秒級失敗直接走完整下載
        sec_opts = dict(base_opts)
        sec_opts["format"] = "bestaudio[protocol=https][abr<=96]/bestaudio[protocol=https]"
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

    return out


# =============================================================
# 計費版 v2 階段二:上傳短音檔 → 後端轉錄 → 扣點
#
# 金鑰只在後端環境變數(未設定時以下端點回錯,但不影響免費版啟動):
#   OPENAI_API_KEY / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
# =============================================================
import json
import math
import urllib.request
import urllib.error

from fastapi import File, Form, Header, UploadFile
from pydantic import BaseModel

MAX_CLIP_UPLOAD_SEC = 10 * 60          # 階段二短音檔上限 10 分鐘
WHISPER_USD_PER_MIN = 0.006            # Whisper 定價(供成本估算/監控)
_jwks_cache: dict = {"keys": None, "at": 0.0}
_jwks_lock = threading.Lock()


def _paid_env():
    """付費相關環境變數(延遲讀取,未設定回 None 讓端點回友善錯誤)"""
    return (
        os.environ.get("OPENAI_API_KEY"),
        (os.environ.get("SUPABASE_URL") or "").rstrip("/"),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
    )


def _get_jwks(supabase_url: str) -> list:
    """取 Supabase 專案的 JWKS 公鑰(快取 1 小時),用來驗證 ES256 JWT"""
    now = time.time()
    with _jwks_lock:
        if _jwks_cache["keys"] is not None and now - _jwks_cache["at"] < 3600:
            return _jwks_cache["keys"]
    url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        keys = data.get("keys") or []
    except Exception as e:
        raise HTTPException(503, f"無法取得驗證金鑰:{str(e)[:120]}")
    with _jwks_lock:
        _jwks_cache["keys"] = keys
        _jwks_cache["at"] = now
    return keys


def _verify_jwt(authorization: str | None) -> str:
    """驗證 Authorization: Bearer <supabase JWT>,回傳 user_id(sub)"""
    import jwt as pyjwt  # PyJWT[crypto]
    from jwt import PyJWKClient, algorithms

    _, supabase_url, _ = _paid_env()
    if not supabase_url:
        raise HTTPException(503, "伺服器尚未設定 SUPABASE_URL")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "請先登入")
    token = authorization.split(" ", 1)[1].strip()
    try:
        header = pyjwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(401, "登入憑證無效")
    kid = header.get("kid")
    alg = header.get("alg", "ES256")
    keys = _get_jwks(supabase_url)
    jwk = next((k for k in keys if k.get("kid") == kid), None)
    if jwk is None:
        # 快取可能過期(專案輪替金鑰),強制刷新一次再找
        with _jwks_lock:
            _jwks_cache["keys"] = None
        jwk = next((k for k in _get_jwks(supabase_url) if k.get("kid") == kid), None)
    if jwk is None:
        raise HTTPException(401, "登入憑證無效(找不到對應金鑰)")
    try:
        key = algorithms.get_default_algorithms()[alg].from_jwk(json.dumps(jwk))
        claims = pyjwt.decode(token, key=key, algorithms=[alg], audience="authenticated")
    except Exception as e:
        raise HTTPException(401, f"登入已過期或無效:{str(e)[:100]}")
    uid = claims.get("sub")
    if not uid:
        raise HTTPException(401, "登入憑證缺少使用者資訊")
    return uid


def _sb_rpc(fn: str, payload: dict):
    """以 service_role 呼叫 Supabase REST RPC(繞過 RLS);回傳 JSON"""
    _, supabase_url, service_key = _paid_env()
    if not supabase_url or not service_key:
        raise HTTPException(503, "伺服器尚未設定 Supabase 服務金鑰")
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{supabase_url}/rest/v1/rpc/{fn}",
        data=body,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="ignore")[:200]
        raise HTTPException(502, f"帳務服務錯誤:{detail}")
    except Exception as e:
        raise HTTPException(502, f"帳務服務連線失敗:{str(e)[:120]}")


def _sb_balance(user_id: str) -> int:
    """以 service_role 讀某使用者的剩餘 credits"""
    _, supabase_url, service_key = _paid_env()
    if not supabase_url or not service_key:
        raise HTTPException(503, "伺服器尚未設定 Supabase 服務金鑰")
    url = (f"{supabase_url}/rest/v1/credit_balances"
           f"?user_id=eq.{user_id}&select=remaining_credits")
    req = urllib.request.Request(url, headers={
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read().decode() or "[]")
    except Exception as e:
        raise HTTPException(502, f"讀取額度失敗:{str(e)[:120]}")
    return int(rows[0]["remaining_credits"]) if rows else 0


def _sb_role(user_id: str) -> str:
    """以 service_role 讀某使用者的 role(user / admin)"""
    _, supabase_url, service_key = _paid_env()
    if not supabase_url or not service_key:
        raise HTTPException(503, "伺服器尚未設定 Supabase 服務金鑰")
    url = f"{supabase_url}/rest/v1/profiles?id=eq.{user_id}&select=role"
    req = urllib.request.Request(url, headers={
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read().decode() or "[]")
    except Exception as e:
        raise HTTPException(502, f"讀取身分失敗:{str(e)[:120]}")
    return (rows[0].get("role") if rows else None) or "user"


def _ffprobe_seconds(path: str) -> float:
    """後端量測音訊長度(絕不信任前端),回傳秒數"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=60, check=True,
        )
        return float((out.stdout or b"0").decode().strip() or 0)
    except Exception:
        raise HTTPException(400, "無法讀取音檔長度,請確認是有效的音訊檔")


def _whisper_transcribe(audio_path: str, api_key: str) -> str:
    """呼叫 OpenAI Whisper 轉錄(以後端金鑰),回傳純文字"""
    import httpx
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.m4a", f, "audio/mp4")}
        data = {"model": "whisper-1", "language": "zh", "response_format": "text"}
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files, data=data, timeout=600,
            )
        except Exception as e:
            raise HTTPException(502, f"轉錄服務連線失敗:{str(e)[:120]}")
    if resp.status_code != 200:
        raise HTTPException(502, f"轉錄失敗:{resp.text[:200]}")
    return resp.text.strip()


# ---- GPT 校正(可選,便宜模型;成本小,MVP 不額外收費)----
CORRECTION_MODEL = os.environ.get("CORRECTION_MODEL", "gpt-4o-mini")
DEFAULT_VOCAB = ("輿情, 行政院, 災害防救, 新聞稿, 記者會, 質詢, 陳情, 簽呈, 公文, "
                 "府會聯絡, 局處, 科長, 專門委員, 參事, 主任秘書")
_CORRECT_SYS = (
    "你是逐字稿校對員。以下是一份語音辨識產生的華語逐字稿,請依規則校正:\n"
    "1. 只能修正錯字、同音誤植、標點符號與分段,絕對不得改寫、潤飾、增加或刪除任何語句內容。\n"
    "2. 以下詞庫是本領域的正確用詞,發現同音或形近的誤植時,修正為詞庫寫法:\n" + DEFAULT_VOCAB + "\n"
    "3. 保留所有 [時:分:秒] 時間標記於原位,不得移動或刪除。\n"
    "4. 使用台灣慣用的繁體中文與全形標點。\n"
    "直接輸出校正後全文,不要加任何說明、前言或 Markdown 標記。"
)


def _chunk_text(text: str, max_len: int = 6000) -> list:
    """依段落切塊,單塊不超過 max_len 字,供 GPT 逐塊校正"""
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) > max_len:
            chunks.append(buf)
            buf = ""
        buf = (buf + "\n\n" + para) if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _gpt_correct(text: str, api_key: str) -> str:
    """用 GPT 逐塊校正逐字稿(修錯字/標點,不改內容);失敗由呼叫端決定退回原文"""
    import httpx
    out = []
    for chunk in _chunk_text(text):
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": CORRECTION_MODEL, "temperature": 0,
                  "messages": [{"role": "system", "content": _CORRECT_SYS},
                               {"role": "user", "content": chunk}]},
            timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"校正失敗:{resp.text[:150]}")
        out.append((resp.json()["choices"][0]["message"]["content"] or "").strip())
    return "\n\n".join(out).strip()


def _free_daily_credits() -> int:
    """每日免費額度(credits/秒),由 FREE_DAILY_MINUTES 設定;0 = 關閉"""
    try:
        return max(0, int(os.environ.get("FREE_DAILY_MINUTES", "0"))) * 60
    except ValueError:
        return 0


def _free_remaining(uid: str) -> int:
    """今日剩餘免費 credits(秒)"""
    limit = _free_daily_credits()
    if limit <= 0:
        return 0
    try:
        res = _sb_rpc("free_remaining", {"p_user_id": uid, "p_free_limit": limit})
        if isinstance(res, list):
            res = res[0] if res else 0
        return int(res or 0)
    except Exception:
        return 0


@app.get("/api/me")
def api_me(authorization: str | None = Header(None)):
    uid = _verify_jwt(authorization)
    credits = _sb_balance(uid)
    free = _free_remaining(uid)
    return {"user_id": uid, "role": _sb_role(uid),
            "remaining_credits": credits, "remaining_minutes": credits // 60,
            "free_remaining_credits": free, "free_remaining_minutes": free // 60,
            "free_daily_minutes": _free_daily_credits() // 60}


class _AddCreditsBody(BaseModel):
    email: str
    minutes: int
    reason: str | None = None
    idempotency_key: str | None = None


@app.post("/api/admin/add-credits")
def api_admin_add_credits(body: _AddCreditsBody, authorization: str | None = Header(None)):
    _, supabase_url, service_key = _paid_env()
    if not (supabase_url and service_key):
        raise HTTPException(503, "服務尚未設定(缺 Supabase 金鑰)")
    uid = _verify_jwt(authorization)
    if _sb_role(uid) != "admin":            # 後端再次把關,絕不信任前端
        raise HTTPException(403, "沒有權限:僅管理員可加值")
    if not body.email or body.minutes is None or body.minutes <= 0:
        raise HTTPException(400, "請提供有效的 Email 與正整數分鐘數")
    credits = int(body.minutes) * 60        # 前端以分鐘輸入,存為 credits(秒)
    res = _sb_rpc("admin_add_credits", {
        "p_caller": uid, "p_email": body.email.strip(), "p_amount": credits,
        "p_reason": (body.reason or "").strip() or "admin 加值",
        "p_idem": body.idempotency_key or "",
    })
    row = res[0] if isinstance(res, list) else res
    if not row or not row.get("ok"):
        reason = (row or {}).get("reason")
        if reason == "user_not_found":
            raise HTTPException(404, "找不到這個 Email 的使用者(對方需先登入過一次)")
        if reason == "not_admin":
            raise HTTPException(403, "沒有權限:僅管理員可加值")
        if reason == "invalid_amount":
            raise HTTPException(400, "分鐘數不正確")
        raise HTTPException(502, "加值失敗,請稍後再試")
    remaining = row.get("remaining") or 0
    return {"ok": True, "target_email": row.get("target_email"),
            "remaining_credits": remaining, "remaining_minutes": int(remaining) // 60,
            "duplicate": row.get("reason") == "duplicate"}


@app.post("/api/transcribe")
def api_transcribe(
    request: Request,
    file: UploadFile = File(...),
    authorization: str | None = Header(None),
):
    api_key, supabase_url, service_key = _paid_env()
    if not (api_key and supabase_url and service_key):
        raise HTTPException(503, "付費轉錄尚未啟用(伺服器未設定金鑰)")
    rate_check("audio", request)
    uid = _verify_jwt(authorization)

    if not _audio_slots.acquire(blocking=False):
        raise HTTPException(429, "伺服器忙碌中,請稍候一分鐘再試")
    tmpdir = tempfile.mkdtemp(prefix="ytpaid-")
    usage_id = None
    try:
        # 1) 存上傳檔(限制大小,邊寫邊擋超量)
        raw = os.path.join(tmpdir, "upload")
        size = 0
        with open(raw, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(413, "檔案超過 100 MB 上限")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "沒有收到音檔")

        # 2) 後端量測長度並檢查上限
        duration = _ffprobe_seconds(raw)
        if duration <= 0:
            raise HTTPException(400, "音檔長度為 0 或無法辨識")
        if duration > MAX_CLIP_UPLOAD_SEC:
            raise HTTPException(400, f"目前僅支援 {MAX_CLIP_UPLOAD_SEC // 60} 分鐘內的短音檔")
        cost = int(math.ceil(duration))       # 1 秒 = 1 credit,預扣無條件進位

        # 3) 原子預扣(service_role RPC)
        res = _sb_rpc("reserve_transcription", {
            "p_user_id": uid, "p_cost": cost,
            "p_source_type": "upload", "p_source_name": (file.filename or "")[:200],
            "p_duration": cost, "p_free_limit": _free_daily_credits(),
        })
        row = res[0] if isinstance(res, list) else res
        if not row or not row.get("ok"):
            reason = (row or {}).get("reason")
            if reason == "insufficient_credits":
                raise HTTPException(402, "剩餘額度不足,請儲值後再試")
            if reason == "active_job_exists":
                raise HTTPException(409, "已有一個轉錄任務進行中,請待完成後再試")
            raise HTTPException(502, "預扣額度失敗,請稍後再試")
        usage_id = row["usage_id"]

        # 4) 轉 16k 單聲道 → Whisper 轉錄
        m4a = os.path.join(tmpdir, "audio.m4a")
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", raw,
                 "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 "-movflags", "+faststart", m4a],
                check=True, timeout=600, capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"音訊轉檔失敗:{(e.stderr or b'').decode(errors='ignore')[:200]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "音訊轉檔逾時")

        text = _whisper_transcribe(m4a, api_key)

        # 5) 結算(實際秒數;Whisper 成本估算供監控)
        cost_usd = round(duration / 60.0 * WHISPER_USD_PER_MIN, 6)
        settle = _sb_rpc("complete_transcription", {
            "p_usage_id": usage_id, "p_user_id": uid,
            "p_actual_seconds": int(math.ceil(duration)), "p_cost_usd": cost_usd,
        })
        srow = settle[0] if isinstance(settle, list) else settle
        remaining = (srow or {}).get("remaining")
        if remaining is None:                 # 極少數:結算沒回餘額才補讀一次
            remaining = _sb_balance(uid)
        usage_id = None  # 已結算,finally 不再退款
        return {"text": text, "remaining_credits": remaining,
                "remaining_minutes": int(remaining) // 60}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"轉錄失敗:{str(e)[:200]}")
    finally:
        # 任務未結算就退回預扣(失敗退款,不吃使用者額度)
        if usage_id is not None:
            try:
                _sb_rpc("fail_transcription", {
                    "p_usage_id": usage_id, "p_user_id": uid, "p_reason": "server_error",
                })
            except Exception:
                pass
        _audio_slots.release()
        shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================
# 計費版 v2 階段三:YouTube 來源 + 長音檔(背景任務 + 進度輪詢)
#
# 需額外環境變數 HOME_RELAY_URL(家用中繼 ngrok 網址,供 YouTube 下載;
# 未設定則 YouTube 來源停用,上傳仍可用)。
# =============================================================
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

MAX_JOB_SEC = 3 * 3600         # 長音檔上限 3 小時
CHUNK_SEC = 540                # 每段 9 分鐘(16k 單聲道 32k ≈ 2MB,遠小於 Whisper 25MB)
CHUNK_WORKERS = 3              # 同一任務內分段並行轉錄數
JOBS_PER_HOUR_PER_USER = 40   # 每使用者每小時建立任務上限(防濫用)
WATCHDOG_MAX_SEC = 4 * 3600   # 任務逾此時長仍 processing 視為卡住(> 3 小時上限)
_v3_jobs: dict = {}            # job_id(=usage_log id) -> 狀態
_v3_lock = threading.Lock()
_user_hits: dict = defaultdict(deque)     # uid -> deque[timestamp]
_user_rate_lock = threading.Lock()


def _user_rate_check(uid: str):
    """每使用者每小時建立任務上限(防濫用;免費額度濫用的第二道防線)"""
    now = time.time()
    with _user_rate_lock:
        dq = _user_hits[uid]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= JOBS_PER_HOUR_PER_USER:
            raise HTTPException(429, "使用太頻繁,請稍後再試")
        dq.append(now)


def _job_set(job_id, **kw):
    with _v3_lock:
        j = _v3_jobs.get(job_id)
        if j is not None:
            j.update(kw)


def _fmt_hms(sec: int) -> str:
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def _relay_fetch_audio(url: str, start, end, dest: str) -> None:
    """向家用中繼 /audio 取回(已截取、已轉檔的)m4a,住宅 IP 負責下載"""
    relay = (os.environ.get("HOME_RELAY_URL") or "").rstrip("/")
    if not relay:
        raise HTTPException(503, "YouTube 來源尚未啟用(伺服器未設定家用中繼網址)")
    q = "/audio?url=" + urllib.parse.quote(url, safe="")
    if start is not None:
        q += f"&start={start}"
    if end is not None:
        q += f"&end={end}"
    req = urllib.request.Request(relay + q, headers={"ngrok-skip-browser-warning": "1"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="ignore")[:200]
        raise HTTPException(502, f"YouTube 下載失敗:{detail}")
    except Exception as e:
        raise HTTPException(502, f"YouTube 下載連線失敗(家用電腦是否開著?):{str(e)[:120]}")


def _transcode_and_segment(src: str, tmpdir: str) -> list:
    """轉 16k 單聲道後,依 CHUNK_SEC 切段,回傳依序的段檔清單"""
    conv = os.path.join(tmpdir, "conv.m4a")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", conv],
        check=True, timeout=1800, capture_output=True,
    )
    pat = os.path.join(tmpdir, "seg_%04d.m4a")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", conv,
         "-f", "segment", "-segment_time", str(CHUNK_SEC), "-c", "copy", pat],
        check=True, timeout=1800, capture_output=True,
    )
    return sorted(glob.glob(os.path.join(tmpdir, "seg_*.m4a")))


def _run_job(job_id, uid, src_path, duration, tmpdir, api_key, do_correct=False):
    """背景執行:切段 → 分批轉錄 → 合併 →(可選)GPT 校正 → 結算;失敗退款。"""
    try:
        _job_set(job_id, status="processing", stage="轉檔切段中", progress=0)
        segs = _transcode_and_segment(src_path, tmpdir)
        n = len(segs)
        if n == 0:
            raise RuntimeError("切段失敗,沒有可轉錄的音訊")
        texts = [None] * n
        done = [0]
        prog_lock = threading.Lock()

        def work(i):
            texts[i] = _whisper_transcribe(segs[i], api_key)
            with prog_lock:
                done[0] += 1
                _job_set(job_id, stage="轉錄中", progress=int(done[0] * 100 / n))

        _job_set(job_id, stage="轉錄中", progress=0)
        errs = []
        with ThreadPoolExecutor(max_workers=CHUNK_WORKERS) as ex:
            futs = [ex.submit(work, i) for i in range(n)]
            for f in futs:
                try:
                    f.result()
                except Exception as e:
                    errs.append(e)
        if errs:
            raise errs[0]

        parts = []
        for i, t in enumerate(texts):
            marker = _fmt_hms(i * CHUNK_SEC)
            parts.append((marker + " " + (t or "")).strip())
        full = "\n\n".join(parts).strip()

        # 可選:GPT 校正(修錯字/標點);校正失敗就退回未校正原文,不讓整筆失敗
        if do_correct and full:
            _job_set(job_id, stage="校正中", progress=100)
            try:
                full = _gpt_correct(full, api_key)
            except Exception:
                pass

        cost_usd = round(duration / 60.0 * WHISPER_USD_PER_MIN, 6)
        settle = _sb_rpc("complete_transcription", {
            "p_usage_id": job_id, "p_user_id": uid,
            "p_actual_seconds": int(math.ceil(duration)), "p_cost_usd": cost_usd,
            "p_result_text": full,
        })
        srow = settle[0] if isinstance(settle, list) else settle
        remaining = (srow or {}).get("remaining")
        if remaining is None:
            remaining = _sb_balance(uid)
        _job_set(job_id, status="done", progress=100, text=full, remaining=remaining)
    except Exception as e:
        try:
            _sb_rpc("fail_transcription", {
                "p_usage_id": job_id, "p_user_id": uid, "p_reason": "job_error"})
        except Exception:
            pass
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        _job_set(job_id, status="failed", error=str(detail)[:200])
    finally:
        _audio_slots.release()
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/jobs")
def api_create_job(
    request: Request,
    file: UploadFile | None = File(None),
    youtube_url: str | None = Form(None),
    start: float | None = Form(None),
    end: float | None = Form(None),
    correct: bool = Form(False),
    authorization: str | None = Header(None),
):
    api_key, supabase_url, service_key = _paid_env()
    if not (api_key and supabase_url and service_key):
        raise HTTPException(503, "付費轉錄尚未啟用(伺服器未設定金鑰)")
    rate_check("audio", request)
    uid = _verify_jwt(authorization)
    _user_rate_check(uid)

    if not _audio_slots.acquire(blocking=False):
        raise HTTPException(429, "伺服器忙碌中,請稍候一分鐘再試")
    tmpdir = tempfile.mkdtemp(prefix="ytpaid-")
    src = os.path.join(tmpdir, "src")
    started = False
    usage_id = None
    try:
        if youtube_url:
            check_url(youtube_url)
            _relay_fetch_audio(youtube_url, start, end, src)
            source_type, source_name = "youtube", youtube_url[:200]
        elif file is not None:
            size = 0
            with open(src, "wb") as out:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, "檔案超過 100 MB 上限")
                    out.write(chunk)
            if size == 0:
                raise HTTPException(400, "沒有收到音檔")
            source_type, source_name = "upload", (file.filename or "")[:200]
        else:
            raise HTTPException(400, "請提供音檔或 YouTube 網址")

        duration = _ffprobe_seconds(src)
        if duration <= 0:
            raise HTTPException(400, "音檔長度為 0 或無法辨識")
        if duration > MAX_JOB_SEC:
            raise HTTPException(400, f"超過 {MAX_JOB_SEC // 3600} 小時上限,請縮短範圍")
        cost = int(math.ceil(duration))

        res = _sb_rpc("reserve_transcription", {
            "p_user_id": uid, "p_cost": cost,
            "p_source_type": source_type, "p_source_name": source_name, "p_duration": cost,
            "p_free_limit": _free_daily_credits(),
        })
        row = res[0] if isinstance(res, list) else res
        if not row or not row.get("ok"):
            reason = (row or {}).get("reason")
            if reason == "insufficient_credits":
                raise HTTPException(402, "剩餘額度不足,請儲值後再試")
            if reason == "active_job_exists":
                raise HTTPException(409, "已有一個轉錄任務進行中,請待完成後再試")
            raise HTTPException(502, "預扣額度失敗,請稍後再試")
        usage_id = row["usage_id"]

        with _v3_lock:
            _v3_jobs[usage_id] = {"status": "processing", "stage": "準備中",
                                  "progress": 0, "uid": uid}
        threading.Thread(target=_run_job,
                         args=(usage_id, uid, src, duration, tmpdir, api_key, correct),
                         daemon=True).start()
        started = True                      # 交棒給背景執行緒:槽與暫存由它清理
        return {"job_id": usage_id, "duration_seconds": int(math.ceil(duration))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"建立任務失敗:{str(e)[:200]}")
    finally:
        if not started:
            if usage_id is not None:
                try:
                    _sb_rpc("fail_transcription", {
                        "p_usage_id": usage_id, "p_user_id": uid, "p_reason": "start_failed"})
                except Exception:
                    pass
            _audio_slots.release()
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str, authorization: str | None = Header(None)):
    _, supabase_url, service_key = _paid_env()
    if not (supabase_url and service_key):
        raise HTTPException(503, "服務尚未設定")
    uid = _verify_jwt(authorization)

    with _v3_lock:
        j = _v3_jobs.get(job_id)
        j = dict(j) if j else None

    if j is not None:
        if j.get("uid") != uid:
            raise HTTPException(403, "沒有權限")
        st = j.get("status")
        if st == "done":
            with _v3_lock:
                _v3_jobs.pop(job_id, None)
            try:                            # 清掉 DB 暫存逐字稿(去內容化)
                _sb_rpc("claim_result", {"p_usage_id": job_id, "p_user_id": uid})
            except Exception:
                pass
            rem = j.get("remaining") or 0
            return {"status": "done", "progress": 100, "text": j.get("text", ""),
                    "remaining_credits": rem, "remaining_minutes": int(rem) // 60}
        if st == "failed":
            with _v3_lock:
                _v3_jobs.pop(job_id, None)
            return {"status": "failed", "error": j.get("error", "轉錄失敗")}
        return {"status": "processing", "stage": j.get("stage", ""), "progress": j.get("progress", 0)}

    # 不在記憶體:可能已被別的輪詢取走,或伺服器重啟造成孤兒任務
    res = _sb_rpc("claim_result", {"p_usage_id": job_id, "p_user_id": uid})
    row = (res[0] if res else None) if isinstance(res, list) else res
    if not row:
        raise HTTPException(404, "找不到這個任務")
    status = row.get("status")
    if status == "completed":
        text = row.get("result_text")
        if text is not None:
            bal = _sb_balance(uid)
            return {"status": "done", "progress": 100, "text": text,
                    "remaining_credits": bal, "remaining_minutes": bal // 60}
        return {"status": "done", "progress": 100, "text": "",
                "note": "結果已取走"}
    if status in ("reserved", "processing"):
        try:
            _sb_rpc("fail_transcription", {
                "p_usage_id": job_id, "p_user_id": uid, "p_reason": "server_restart"})
        except Exception:
            pass
        return {"status": "failed", "error": "伺服器重新啟動,任務中斷,已退款,請重試"}
    return {"status": "failed", "error": "任務已結束(失敗或已退款)"}


def _cleanup_loop():
    """每小時清掉逾 24 小時的暫存目錄(零保存兜底)"""
    while True:
        time.sleep(3600)
        try:
            now = time.time()
            root = tempfile.gettempdir()
            for name in os.listdir(root):
                if name.startswith(("ytpaid-", "ytaudio-", "ytcap-")):
                    p = os.path.join(root, name)
                    try:
                        if now - os.path.getmtime(p) > 24 * 3600:
                            shutil.rmtree(p, ignore_errors=True)
                    except OSError:
                        pass
        except Exception:
            pass


def _watchdog_loop():
    """每 15 分鐘掃 DB:卡住(逾 4 小時仍 processing/reserved)的任務自動失敗退款。
    對應規畫書 11.2:救回因當機/重啟/掛住而沒走完流程、又沒被使用者輪詢到的任務。"""
    while True:
        time.sleep(900)
        try:
            _, supabase_url, service_key = _paid_env()
            if not (supabase_url and service_key):
                continue
            threshold = (datetime.now(timezone.utc)
                         - timedelta(seconds=WATCHDOG_MAX_SEC)).strftime("%Y-%m-%dT%H:%M:%S")
            url = (f"{supabase_url}/rest/v1/usage_logs"
                   f"?status=in.(reserved,processing)&created_at=lt.{threshold}"
                   f"&select=id,user_id")
            req = urllib.request.Request(url, headers={
                "apikey": service_key, "Authorization": f"Bearer {service_key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode() or "[]")
            for row in rows:
                jid, u = row.get("id"), row.get("user_id")
                with _v3_lock:
                    if jid in _v3_jobs:      # 本機仍在跑,不動它
                        continue
                try:
                    _sb_rpc("fail_transcription", {
                        "p_usage_id": jid, "p_user_id": u, "p_reason": "watchdog_timeout"})
                except Exception:
                    pass
        except Exception:
            pass


threading.Thread(target=_cleanup_loop, daemon=True).start()
threading.Thread(target=_watchdog_loop, daemon=True).start()


# =============================================================
# 計費版 v2 金流:綠界 ECPay 全方位金流(AIO)線上儲值
#
# 環境變數(未設定則用綠界「公開測試值」,可直接在測試環境試):
#   ECPAY_MERCHANT_ID / ECPAY_HASH_KEY / ECPAY_HASH_IV
#   ECPAY_ENV(stage=測試 / production=正式)
#   PUBLIC_BASE_URL(本後端公開網址,組 ReturnURL)
#   SITE_V2_URL(前端 v2 網址,付款後導回)
# =============================================================
import hashlib
import random

# 綠界公開測試帳號(官方文件提供,任何人可用於測試環境)
_ECPAY_TEST = {"mid": "2000132", "key": "5294y06JbISpM5x9", "iv": "v77hoKGq4kWxNNIS"}
_ECPAY_AIO = {
    "stage": "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5",
    "production": "https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5",
}

# 儲值方案(價格由營運者自行調整;1 分鐘 = 60 credits)
PAY_PACKAGES = [
    {"id": "p100", "amount_twd": 100, "minutes": 60},
    {"id": "p300", "amount_twd": 300, "minutes": 200},
    {"id": "p500", "amount_twd": 500, "minutes": 360},
]


def _ecpay_conf():
    mid = os.environ.get("ECPAY_MERCHANT_ID") or _ECPAY_TEST["mid"]
    key = os.environ.get("ECPAY_HASH_KEY") or _ECPAY_TEST["key"]
    iv = os.environ.get("ECPAY_HASH_IV") or _ECPAY_TEST["iv"]
    env = os.environ.get("ECPAY_ENV", "stage")
    return mid, key, iv, ("production" if env == "production" else "stage")


def _ecpay_checkmac(params: dict) -> str:
    """依綠界規格計算 CheckMacValue(EncryptType=1,SHA256)"""
    _, key, iv, _ = _ecpay_conf()
    items = sorted(((k, v) for k, v in params.items() if k != "CheckMacValue"),
                   key=lambda x: x[0].lower())
    raw = "HashKey=" + key + "&" + "&".join(f"{k}={v}" for k, v in items) + "&HashIV=" + iv
    enc = urllib.parse.quote_plus(raw).lower()
    for a, b in [("%2d", "-"), ("%5f", "_"), ("%2e", "."), ("%21", "!"),
                 ("%2a", "*"), ("%28", "("), ("%29", ")"), ("%20", "+")]:
        enc = enc.replace(a, b)
    return hashlib.sha256(enc.encode()).hexdigest().upper()


def _sb_insert_payment(user_id, amount, credits, mtn):
    """建立一筆 pending 付款,回傳 id"""
    _, supabase_url, service_key = _paid_env()
    body = json.dumps({
        "user_id": user_id, "provider": "ecpay", "amount": amount, "currency": "TWD",
        "credits_added": credits, "status": "pending", "merchant_trade_no": mtn,
    }).encode()
    req = urllib.request.Request(
        f"{supabase_url}/rest/v1/payments", data=body, method="POST",
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}",
                 "Content-Type": "application/json", "Prefer": "return=representation"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read().decode() or "[]")
    return rows[0]["id"]


def _sb_payment_by_mtn(mtn: str):
    """以 MerchantTradeNo 找付款(回傳 dict 或 None)"""
    _, supabase_url, service_key = _paid_env()
    url = (f"{supabase_url}/rest/v1/payments?merchant_trade_no=eq."
           f"{urllib.parse.quote(mtn, safe='')}&select=id,status,credits_added")
    req = urllib.request.Request(url, headers={
        "apikey": service_key, "Authorization": f"Bearer {service_key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read().decode() or "[]")
    return rows[0] if rows else None


@app.get("/api/pay/packages")
def api_pay_packages():
    return {"packages": PAY_PACKAGES, "currency": "TWD"}


@app.post("/api/pay/create")
def api_pay_create(request: Request, package_id: str = Form(...),
                   authorization: str | None = Header(None)):
    _, supabase_url, service_key = _paid_env()
    if not (supabase_url and service_key):
        raise HTTPException(503, "服務尚未設定")
    uid = _verify_jwt(authorization)
    pkg = next((p for p in PAY_PACKAGES if p["id"] == package_id), None)
    if not pkg:
        raise HTTPException(400, "無效的方案")

    mid, _, _, env = _ecpay_conf()
    credits = pkg["minutes"] * 60
    # MerchantTradeNo:≤20 英數且唯一
    mtn = ("T" + format(int(time.time() * 1000), "x")
           + "".join(random.choice("0123456789abcdef") for _ in range(4)))[:20]
    payment_id = _sb_insert_payment(uid, pkg["amount_twd"], credits, mtn)

    base = (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    params = {
        "MerchantID": mid,
        "MerchantTradeNo": mtn,
        "MerchantTradeDate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y/%m/%d %H:%M:%S"),
        "PaymentType": "aio",
        "TotalAmount": str(pkg["amount_twd"]),
        "TradeDesc": "credits topup",
        "ItemName": f"轉錄額度 {pkg['minutes']} 分鐘",
        "ReturnURL": base + "/api/pay/ecpay/callback",
        "ClientBackURL": base + "/api/pay/ecpay/return",
        "ChoosePayment": "ALL",
        "EncryptType": "1",
    }
    params["CheckMacValue"] = _ecpay_checkmac(params)
    return {"action": _ECPAY_AIO[env], "params": params, "payment_id": payment_id}


@app.post("/api/pay/ecpay/callback")
async def api_pay_callback(request: Request):
    """綠界伺服器對伺服器付款結果通知;驗章成功且付款成功才入帳,回應 1|OK"""
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    mac = data.get("CheckMacValue", "")
    if not mac or _ecpay_checkmac(data) != mac.upper():
        return PlainTextResponse("0|CheckMacValue error", status_code=400)
    if data.get("RtnCode") != "1":
        return PlainTextResponse("1|OK")   # 收到但非成功,不入帳
    mtn = data.get("MerchantTradeNo", "")
    pay = _sb_payment_by_mtn(mtn)
    if not pay:
        return PlainTextResponse("0|order not found", status_code=400)
    try:
        _sb_rpc("credit_payment", {
            "p_payment_id": pay["id"],
            "p_provider_txn": data.get("TradeNo") or mtn,
            "p_raw": data,
        })
    except Exception:
        return PlainTextResponse("0|credit error", status_code=500)
    return PlainTextResponse("1|OK")


@app.get("/api/pay/ecpay/return")
def api_pay_return():
    """使用者付款後瀏覽器導回 → 轉回前端頁(入帳以 callback 為準)"""
    site = os.environ.get("SITE_V2_URL") or "../"
    sep = "&" if "?" in site else "?"
    return RedirectResponse(site + sep + "paid=1")
