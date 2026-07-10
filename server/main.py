"""
YouTube 音訊擷取服務(供「語音轉逐字稿」前端使用)

只做一件事:把 YouTube 影片(或指定的起訖片段)抓下來、壓成最小的
16kHz 單聲道 32kbps AAC 音訊回傳給瀏覽器,瀏覽器再用使用者自己的
OpenAI API Key 走 Whisper 轉錄。

零保存原則:暫存檔寫入系統暫存目錄,回應送出後立即刪除;
伺服器不保存任何音訊、不經手任何 API Key。
"""

import hmac
import ipaddress
import os
import re
import glob
import secrets
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


def get_trusted_client_ip(request: Request) -> str:
    """可信來源 IP:只取反向代理(Render)附加的 XFF「最右值」,並以
    ipaddress 驗證;不合法或缺頭時退回連線層 IP。最左值由用戶端任填,
    取之會被偽造繞過限流與試用額度。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        last = fwd.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass  # 不合法的 header 不可當成合法身分,退回連線層
    return request.client.host if request.client else "unknown"


client_ip = get_trusted_client_ip  # 既有呼叫端沿用舊名


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


# =============================================================
# 環境與中繼防護
#   APP_ENV=production 時:ECPay 不退回測試金鑰、PUBLIC_BASE_URL 必填。
#   家用中繼(server/home)設 RELAY_REQUIRE_AUTH=1 + RELAY_SHARED_SECRET:
#   /info /caption /audio /diag 需通過 HMAC 簽章(timestamp±60s、nonce 防重放)。
#   Render 只設 RELAY_SHARED_SECRET(對外簽章),自身端點維持公開供 byok 使用。
# =============================================================
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# 家用中繼 HMAC 驗證已抽到 relay.py;以舊名沿用(路由於下方呼叫)
from relay import (RELAY_SIG_SKEW_SEC, _relay_nonces, _relay_nonce_lock,
                   _relay_secret, _relay_auth_required, _relay_sign,
                   _relay_sign_headers, relay_guard)


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


def _pot_provider_alive() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


@app.get("/healthz")
def healthz():
    """liveness:只回報 Web 行程存活(依賴狀態請看 /readyz)"""
    return {"ok": True}


@app.get("/readyz")
def readyz():
    """readiness:檢查必要依賴,任何必要項故障回 503(不輸出金鑰或內部路徑)"""
    deps = {
        "ffmpeg": bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")),
        "tmp_writable": False,
        "pot_provider": _pot_provider_alive(),
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "supabase_configured": bool(os.environ.get("SUPABASE_URL")
                                    and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
        "relay_auth": (not _relay_auth_required()) or bool(_relay_secret()),
    }
    try:
        with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir()) as f:
            f.write(b"x")
        deps["tmp_writable"] = True
    except Exception:
        deps["tmp_writable"] = False
    # 必要依賴:ffmpeg、暫存目錄、(啟用中繼驗證時)密鑰、PO Token 產生器
    critical = deps["ffmpeg"] and deps["tmp_writable"] and deps["relay_auth"] and deps["pot_provider"]
    body = {"ok": critical, "env": APP_ENV, "dependencies": deps}
    if not critical:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/diag")
def diag(request: Request, url: str = Query(...)):
    """遠端診斷:回傳 yt-dlp 詳細日誌。僅供站長排查:須設 DIAG_TOKEN 並帶
    X-Diag-Token 標頭;未設 DIAG_TOKEN 一律 404(正式環境預設關閉)。"""
    token = os.environ.get("DIAG_TOKEN", "")
    given = request.headers.get("x-diag-token", "")
    if not token or not hmac.compare_digest(token, given):
        raise HTTPException(404, "Not Found")
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


def _reject_live(meta: dict) -> None:
    """直播/即將直播/無限串流不支援(未知長度不可當 0 秒放行)"""
    if meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise HTTPException(400, "直播影片不支援轉錄,請等影片結束轉為一般影片後再試")


@app.get("/info")
def info(request: Request, url: str = Query(...)):
    relay_guard(request)
    rate_check("info", request)
    check_url(url)
    d = probe(url)
    _reject_live(d)
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
    relay_guard(request)
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
    relay_guard(request)
    rate_check("audio", request)
    check_url(url)
    if start is not None and end is not None and end <= start:
        raise HTTPException(400, "終點必須大於起點")

    meta = probe(url)
    _reject_live(meta)
    duration = int(meta.get("duration") or 0)
    if duration <= 0 and end is None:
        # 未知長度不可視為 0 秒放行:必須指定明確截取範圍
        raise HTTPException(400, "無法取得影片長度,請指定明確的起訖時間再試")
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
import pricing                          # 集中式價格設定與成本估算
WHISPER_USD_PER_MIN = pricing.whisper_per_min()   # 相容舊名(仍供顯示)


def _settle_cost(duration_sec: float, correct_usage: dict | None):
    """依實際用量算整筆成本明細:whisper(秒)+ 校正(tokens)。
    correct_usage=None 代表未做校正;有做但沒拿到 usage 則 tokens_known=False。"""
    pt = int((correct_usage or {}).get("prompt_tokens", 0) or 0)
    ct = int((correct_usage or {}).get("completion_tokens", 0) or 0)
    tokens_known = bool(correct_usage) and (correct_usage.get("calls", 0) > 0)
    return pricing.estimate_job_cost(duration_sec, CORRECTION_MODEL, pt, ct, tokens_known), pt, ct
ALLOWED_JWT_ALGORITHMS = ("ES256",)   # Supabase 簽發演算法,白名單固定
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
    alg = header.get("alg")
    # 演算法白名單固定由伺服器決定,不接受 token header 自報(防 alg=none/降級)
    if alg not in ALLOWED_JWT_ALGORITHMS:
        raise HTTPException(401, "登入憑證無效")
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
        claims = pyjwt.decode(
            token, key=key,
            algorithms=list(ALLOWED_JWT_ALGORITHMS),
            audience="authenticated",
            issuer=f"{supabase_url}/auth/v1",
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except Exception:
        raise HTTPException(401, "登入已過期或無效,請重新登入")
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


def _whisper_transcribe_segments(audio_path: str, api_key: str) -> list:
    """同 _whisper_transcribe,但取 verbose_json 回傳 segments(供時間軸)"""
    import httpx
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.m4a", f, "audio/mp4")}
        data = {"model": "whisper-1", "language": "zh", "response_format": "verbose_json"}
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
    return resp.json().get("segments") or []


def _segments_to_text(segments: list, offset: float = 0, para_sec: int = 30) -> str:
    """segments 以約 para_sec 秒為一段組段,段首加時間戳(格式同付費版 _fmt_hms)"""
    def stamp(sec: float) -> str:
        return _fmt_hms(int(sec))

    paras, buf, para_start = [], [], None
    for seg in segments:
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        start = float(seg.get("start") or 0)
        if para_start is None:
            para_start = start
        buf.append(txt)
        if float(seg.get("end") or start) - para_start >= para_sec:
            paras.append(stamp(para_start + offset) + " " + "".join(buf))
            buf, para_start = [], None
    if buf:
        paras.append(stamp((para_start or 0) + offset) + " " + "".join(buf))
    return "\n\n".join(paras)


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
_CORRECT_RULE_FILLERS = (
    "5.(此為規則 1 的例外)刪除「嗯」「啊」「呃」「就是說」「那個」「這個」等"
    "無意義的口頭贅詞與填充詞;除贅詞外仍不得刪除任何有實際內容的語句。"
)
_CORRECT_RULE_SPEAKERS = (
    "6.(此為規則 1 的例外)依語意推測講者輪替,在每位講者發言開頭加上「甲:」「乙:」"
    "標記(最多兩位);除此標記外仍不得增加任何內容,無法判斷時不要硬標。"
)


def _correct_sys(remove_fillers: bool = False, speakers: bool = False) -> str:
    sys = _CORRECT_SYS
    if remove_fillers:
        sys += "\n" + _CORRECT_RULE_FILLERS
    if speakers:
        sys += "\n" + _CORRECT_RULE_SPEAKERS
    return sys


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


def _gpt_correct(text: str, api_key: str, remove_fillers: bool = False, speakers: bool = False,
                 usage: dict | None = None) -> str:
    """用 GPT 逐塊校正逐字稿(修錯字/標點,不改內容);失敗由呼叫端決定退回原文。
    傳入 usage dict 時,累計 prompt_tokens/completion_tokens(供成本統計)。"""
    import httpx
    out = []
    sys_prompt = _correct_sys(remove_fillers, speakers)
    for chunk in _chunk_text(text):
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": CORRECTION_MODEL, "temperature": 0,
                  "messages": [{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": chunk}]},
            timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"校正失敗:{resp.text[:150]}")
        body = resp.json()
        out.append((body["choices"][0]["message"]["content"] or "").strip())
        if usage is not None:
            u = body.get("usage") or {}
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + int(u.get("prompt_tokens", 0) or 0)
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + int(u.get("completion_tokens", 0) or 0)
            usage["calls"] = usage.get("calls", 0) + 1
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
    _content_length_precheck(request)
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

        # 5) 結算(實際秒數;此端點不做校正,成本=Whisper)
        cost, pt, ct = _settle_cost(duration, None)
        settle = _sb_rpc("complete_transcription", {
            "p_usage_id": usage_id, "p_user_id": uid,
            "p_actual_seconds": int(math.ceil(duration)), "p_cost_usd": cost["total_usd"],
            "p_prompt_tokens": pt, "p_completion_tokens": ct, "p_pricing": cost,
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
# ---- 背景任務佇列(資料庫佇列 + 就地 Worker;Render 免費方案無獨立 Worker 服務)----
try:
    WORKER_CONCURRENCY = max(1, int(os.environ.get("WORKER_CONCURRENCY", "1")))
except ValueError:
    WORKER_CONCURRENCY = 1
JOB_STALE_SEC = 180           # 心跳逾此秒數視為 worker 死亡 → 退回佇列重試
JOB_MAX_RETRY = 2             # 每筆任務最多自動重試次數(逾此則失敗退款)
HEARTBEAT_EVERY = 30          # worker 處理中每隔幾秒更新一次心跳
JOB_SRC_DIR = os.path.join(tempfile.gettempdir(), "jobsrc")   # 上傳檔暫存(worker 讀)
_worker_wake = threading.Event()
_v3_jobs: dict = {}            # job_id(=usage_log id) -> 記憶體即時狀態(僅供進度顯示)
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


def _relay_download_caps():
    try:
        max_bytes = int(os.environ.get("MAX_RELAY_DOWNLOAD_BYTES", str(512 * 1024 * 1024)))
    except ValueError:
        max_bytes = 512 * 1024 * 1024
    try:
        max_sec = int(os.environ.get("MAX_RELAY_DOWNLOAD_SECONDS", "1800"))
    except ValueError:
        max_sec = 1800
    return max_bytes, max_sec


def _relay_fetch_audio(url: str, start, end, dest: str) -> None:
    """向家用中繼 /audio 取回(已截取、已轉檔的)m4a,住宅 IP 負責下載。
    對外附 HMAC 簽章(中繼端驗證);分塊下載並限制總大小與總時間。"""
    relay = (os.environ.get("HOME_RELAY_URL") or "").rstrip("/")
    if not relay:
        raise HTTPException(503, "YouTube 來源尚未啟用(伺服器未設定家用中繼網址)")
    query = {"url": url}
    if start is not None:
        query["start"] = str(start)
    if end is not None:
        query["end"] = str(end)
    q = "/audio?" + "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in query.items())
    # 簽章用「未編碼值」的正規化字串,中繼端以 query_params(已解碼)重算
    headers = {"ngrok-skip-browser-warning": "1"}
    headers.update(_relay_sign_headers("GET", "/audio", query))
    req = urllib.request.Request(relay + q, headers=headers)
    max_bytes, max_sec = _relay_download_caps()
    deadline = time.time() + max_sec
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=1800) as r, open(dest, "wb") as f:
            while True:
                if time.time() > deadline:
                    raise HTTPException(504, "YouTube 下載逾時,請改截取較短片段")
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise HTTPException(413, "下載內容超過大小上限,請改截取較短片段")
                f.write(chunk)
    except HTTPException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
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


class _Cancelled(Exception):
    pass


def _process_job(job_id, uid, jobrow, api_key):
    """Worker 執行一筆任務:取得音訊 → 切段 → 分批轉錄 → 合併 →(可選)校正 → 結算。
    心跳定期更新;偵測到取消即中止。失敗依重試上限退回佇列或失敗退款。"""
    tmpdir = tempfile.mkdtemp(prefix="ytpaid-")
    upload_path = jobrow.get("upload_path")
    hb_stop = threading.Event()
    cancelled = {"v": False}
    terminal = {"v": False}   # True=已結案(可刪上傳檔);requeue 時保留供下次重試

    def heartbeat():
        while not hb_stop.wait(HEARTBEAT_EVERY):
            try:
                st = _rpc1("job_heartbeat", {"p_usage_id": job_id})
                if st == "cancelled":
                    cancelled["v"] = True
                    hb_stop.set()
            except Exception:
                pass

    def ck():
        if cancelled["v"]:
            raise _Cancelled()

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    try:
        _job_set(job_id, status="processing", stage="準備中", progress=0)
        src = os.path.join(tmpdir, "src")
        if jobrow.get("source_type") == "youtube":
            _job_set(job_id, stage="取得音訊中", progress=0)
            _relay_fetch_audio(jobrow.get("youtube_url"),
                               jobrow.get("start_seconds"), jobrow.get("end_seconds"), src)
        else:
            if not upload_path or not os.path.exists(upload_path):
                # 上傳檔已失效(通常是重啟後暫存遺失)→ 不可重試,直接失敗退款
                terminal["v"] = True
                try:
                    _sb_rpc("fail_transcription", {
                        "p_usage_id": job_id, "p_user_id": uid, "p_reason": "upload_lost"})
                except Exception:
                    pass
                _job_set(job_id, status="failed", error="上傳檔已失效,請重新上傳轉錄")
                return
            src = upload_path
        ck()

        duration = jobrow.get("duration_seconds") or _ffprobe_seconds(src)
        _job_set(job_id, stage="轉檔切段中", progress=0)
        segs = _transcode_and_segment(src, tmpdir)
        n = len(segs)
        if n == 0:
            raise RuntimeError("切段失敗,沒有可轉錄的音訊")
        texts = [None] * n
        done = [0]
        prog_lock = threading.Lock()

        def work(i):
            ck()
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
        ck()

        parts = []
        for i, t in enumerate(texts):
            parts.append((_fmt_hms(i * CHUNK_SEC) + " " + (t or "")).strip())
        full = "\n\n".join(parts).strip()

        cu = None
        if jobrow.get("correction_requested") and full:
            _job_set(job_id, stage="校正中", progress=100)
            cu = {}
            try:
                full = _gpt_correct(full, api_key, usage=cu)
            except Exception:
                cu = None   # 校正失敗 → 退回未校正原文,不計校正成本、不讓整筆失敗
        ck()

        cost, pt, ct = _settle_cost(duration, cu)
        settle = _sb_rpc("complete_transcription", {
            "p_usage_id": job_id, "p_user_id": uid,
            "p_actual_seconds": int(math.ceil(duration)), "p_cost_usd": cost["total_usd"],
            "p_result_text": full,
            "p_prompt_tokens": pt, "p_completion_tokens": ct, "p_pricing": cost,
        })
        terminal["v"] = True
        srow = settle[0] if isinstance(settle, list) else settle
        remaining = (srow or {}).get("remaining")
        if remaining is None:
            remaining = _sb_balance(uid)
        _job_set(job_id, status="done", progress=100, text=full, remaining=remaining)
    except _Cancelled:
        terminal["v"] = True   # 使用者取消:退款已由 cancel_job 處理,worker 只需收拾
        _job_set(job_id, status="failed", error="已取消")
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        try:
            outcome = _rpc1("job_retry_or_fail", {
                "p_usage_id": job_id, "p_max_retry": JOB_MAX_RETRY, "p_error": str(detail)[:100]})
        except Exception:
            outcome = None
        if outcome == "requeued":
            _job_set(job_id, status="queued", stage="稍後重試", progress=0)
            _worker_wake.set()
        else:
            terminal["v"] = True
            _job_set(job_id, status="failed", error=str(detail)[:200])
    finally:
        hb_stop.set()
        shutil.rmtree(tmpdir, ignore_errors=True)
        if terminal["v"] and upload_path:
            try:
                os.remove(upload_path)
            except OSError:
                pass


def _worker_loop():
    """就地 Worker:輪詢資料庫佇列,認領 queued 任務並處理。單一 Web 實例內執行。"""
    time.sleep(20)   # 讓依賴先就緒
    while True:
        api_key, supabase_url, service_key = _paid_env()
        if not (api_key and supabase_url and service_key):
            time.sleep(30)
            continue
        try:
            row = _rpc1("claim_next_job", {"p_stale_sec": JOB_STALE_SEC, "p_max_retry": JOB_MAX_RETRY})
        except Exception:
            row = None
        if row and row.get("usage_id"):
            jid = row["usage_id"]
            with _v3_lock:
                if jid not in _v3_jobs:
                    _v3_jobs[jid] = {"status": "processing", "stage": "準備中",
                                     "progress": 0, "uid": row.get("user_id")}
            try:
                _process_job(jid, row.get("user_id"), row, api_key)
            except Exception:
                pass
        else:
            _worker_wake.wait(timeout=10)
            _worker_wake.clear()


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
    _content_length_precheck(request)
    rate_check("audio", request)
    uid = _verify_jwt(authorization)
    _user_rate_check(uid)

    # 只做「驗證輸入 + 取得長度 + 入列」;實際下載/轉錄交給就地 Worker(不阻塞請求)。
    upload_path = None
    yt_start = yt_end = None
    try:
        if youtube_url:
            check_url(youtube_url)
            if start is not None and end is not None and end <= start:
                raise HTTPException(400, "終點必須大於起點")
            meta = probe(youtube_url)
            _reject_live(meta)
            vdur = int(meta.get("duration") or 0)
            s = int(start) if start else 0
            if start is None and end is None:
                if vdur <= 0:
                    raise HTTPException(400, "無法取得影片長度,請指定明確的起訖時間再試")
                if vdur > MAX_JOB_SEC:
                    raise HTTPException(400, f"影片超過 {MAX_JOB_SEC // 3600} 小時,請用起訖時間截取")
                duration = vdur
            else:
                e = int(end) if end is not None else (vdur or 0)
                if e <= 0:
                    raise HTTPException(400, "無法取得影片長度,請指定明確的終點時間")
                duration = e - s
                if duration <= 0:
                    raise HTTPException(400, "截取範圍無效")
                if duration > MAX_CLIP_SEC:
                    raise HTTPException(400, "截取片段超過 2 小時上限,請縮短範圍")
                yt_start, yt_end = s, e
            source_type, source_name = "youtube", youtube_url[:200]
        elif file is not None:
            os.makedirs(JOB_SRC_DIR, exist_ok=True)
            upload_path = os.path.join(JOB_SRC_DIR, secrets.token_hex(16))
            size = 0
            with open(upload_path, "wb") as out:
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
            duration = _ffprobe_seconds(upload_path)
            if duration <= 0:
                raise HTTPException(400, "音檔長度為 0 或無法辨識")
            if duration > MAX_JOB_SEC:
                raise HTTPException(400, f"超過 {MAX_JOB_SEC // 3600} 小時上限,請縮短範圍")
            source_type, source_name = "upload", (file.filename or "")[:200]
        else:
            raise HTTPException(400, "請提供音檔或 YouTube 網址")

        cost = int(math.ceil(duration))
        try:
            res = _sb_rpc("enqueue_transcription", {
                "p_user_id": uid, "p_cost": cost,
                "p_source_type": source_type, "p_source_name": source_name, "p_duration": cost,
                "p_free_limit": _free_daily_credits(),
                "p_youtube_url": youtube_url if source_type == "youtube" else None,
                "p_start": yt_start, "p_end": yt_end,
                "p_upload_path": upload_path, "p_correct": bool(correct),
            })
        except HTTPException as e:
            # migration(schema_phase8)尚未套用時 RPC 不存在 → 給明確訊息(短音檔仍可用)
            if "PGRST202" in str(getattr(e, "detail", "")) or "enqueue_transcription" in str(getattr(e, "detail", "")):
                raise HTTPException(503, "長音檔背景轉錄升級中,暫時無法使用(站長:請套用 v2/schema_phase8.sql)")
            raise
        row = res[0] if isinstance(res, list) else res
        if not row or not row.get("ok"):
            reason = (row or {}).get("reason")
            if reason == "insufficient_credits":
                raise HTTPException(402, "剩餘額度不足,請儲值後再試")
            if reason == "active_job_exists":
                raise HTTPException(409, "已有一個轉錄任務進行中,請待完成後再試")
            raise HTTPException(502, "預扣額度失敗,請稍後再試")
        usage_id = row["usage_id"]
        upload_path = None   # 交棒給 worker:別在 finally 刪掉

        with _v3_lock:
            _v3_jobs[usage_id] = {"status": "queued", "stage": "排隊中",
                                  "progress": 0, "uid": uid}
        _worker_wake.set()   # 叫醒 worker 立即認領
        return {"job_id": usage_id, "duration_seconds": cost}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"建立任務失敗:{str(e)[:200]}")
    finally:
        if upload_path:   # 入列失敗才需清掉暫存上傳檔
            try:
                os.remove(upload_path)
            except OSError:
                pass


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

    # 不在記憶體:可能已被別的輪詢取走,或本實例重啟後由 worker 接手中
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
        return {"status": "done", "progress": 100, "text": "", "note": "結果已取走"}
    if status in ("reserved", "queued", "processing"):
        # 不再因重啟就判失敗:worker 會重新認領/重試,逾時退款由 sweep 處理
        _worker_wake.set()
        return {"status": "processing", "stage": "排隊處理中", "progress": 0}
    if status == "cancelled":
        return {"status": "failed", "error": "已取消"}
    return {"status": "failed", "error": "任務已結束(失敗或已退款)"}


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str, authorization: str | None = Header(None)):
    _, supabase_url, service_key = _paid_env()
    if not (supabase_url and service_key):
        raise HTTPException(503, "服務尚未設定")
    uid = _verify_jwt(authorization)
    res = _sb_rpc("cancel_job", {"p_usage_id": job_id, "p_user_id": uid})
    row = (res[0] if res else None) if isinstance(res, list) else res
    if not row or not row.get("ok"):
        raise HTTPException(409, "任務無法取消(可能已結束)")
    with _v3_lock:
        if job_id in _v3_jobs:
            _v3_jobs[job_id]["status"] = "failed"
            _v3_jobs[job_id]["error"] = "已取消"
    rem = row.get("remaining") or 0
    return {"ok": True, "remaining_credits": rem, "remaining_minutes": int(rem) // 60}


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
            # 孤兒上傳暫存檔(入列失敗殘留或極端狀況)逾 24 小時清掉
            if os.path.isdir(JOB_SRC_DIR):
                for name in os.listdir(JOB_SRC_DIR):
                    p = os.path.join(JOB_SRC_DIR, name)
                    try:
                        if now - os.path.getmtime(p) > 24 * 3600:
                            os.remove(p)
                    except OSError:
                        pass
        except Exception:
            pass


def _watchdog_loop():
    """每 2 分鐘呼叫 sweep_jobs:心跳過期且逾重試上限的任務失敗退款、孤兒 queued 逾時退款、
    過期逐字稿去內容化。心跳/重試由 worker 認領時處理,這裡是最終兜底。"""
    time.sleep(45)   # 讓 worker 先有機會認領
    while True:
        try:
            _, supabase_url, service_key = _paid_env()
            if supabase_url and service_key:
                _sb_rpc("sweep_jobs", {"p_stale_sec": JOB_STALE_SEC, "p_max_retry": JOB_MAX_RETRY})
        except Exception:
            pass
        time.sleep(120)


threading.Thread(target=_cleanup_loop, daemon=True).start()
threading.Thread(target=_watchdog_loop, daemon=True).start()
for _wi in range(WORKER_CONCURRENCY):
    threading.Thread(target=_worker_loop, daemon=True).start()


# =============================================================
# 計費版 v2 金流:綠界 ECPay 全方位金流(AIO)線上儲值
#
# 環境變數(未設定則用綠界「公開測試值」,可直接在測試環境試):
#   ECPAY_MERCHANT_ID / ECPAY_HASH_KEY / ECPAY_HASH_IV
#   ECPAY_ENV(stage=測試 / production=正式)
#   PUBLIC_BASE_URL(本後端公開網址,組 ReturnURL)
#   SITE_V2_URL(前端 v2 網址,付款後導回)
# =============================================================
import random

# 金流純運算/設定已抽到 ecpay.py;此處以舊名沿用(路由與入帳邏輯仍在下方)
from ecpay import PAY_PACKAGES, AIO_URLS as _ECPAY_AIO, ecpay_conf as _ecpay_conf, ecpay_checkmac as _ecpay_checkmac


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
           f"{urllib.parse.quote(mtn, safe='')}&select=id,status,credits_added,amount,provider")
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

    public_base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if not public_base:
        if IS_PRODUCTION:
            # 正式環境不可用使用者可控的 Host header 產生付款回呼網址
            raise HTTPException(503, "金流尚未設定完成(缺 PUBLIC_BASE_URL),暫停儲值")
        public_base = str(request.base_url)
    base = public_base.rstrip("/")
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
    mid, _, _, _ = _ecpay_conf()
    mac = data.get("CheckMacValue", "")
    if not mac or not hmac.compare_digest(_ecpay_checkmac(data), mac.upper()):
        return PlainTextResponse("0|CheckMacValue error", status_code=400)
    if data.get("MerchantID") != mid:
        return PlainTextResponse("0|MerchantID error", status_code=400)
    if data.get("RtnCode") != "1":
        return PlainTextResponse("1|OK")   # 收到但非成功,不入帳
    mtn = data.get("MerchantTradeNo", "")
    pay = _sb_payment_by_mtn(mtn)
    if not pay:
        return PlainTextResponse("0|order not found", status_code=400)
    if (pay.get("provider") or "ecpay") != "ecpay":
        return PlainTextResponse("0|provider error", status_code=400)
    if pay.get("status") not in ("pending", "paid"):
        return PlainTextResponse("0|order state error", status_code=400)
    # 回傳金額必須與本地訂單完全一致,防以小額回呼冒領大額方案
    try:
        if int(float(data.get("TradeAmt", "-1"))) != int(float(pay.get("amount") or -2)):
            return PlainTextResponse("0|amount mismatch", status_code=400)
    except (TypeError, ValueError):
        return PlainTextResponse("0|amount mismatch", status_code=400)
    try:
        # credit_payment 為原子 RPC:已入帳的訂單重送回 duplicate、不重複加值(冪等)
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


# =============================================================
# 免費試用(首頁,免登入):由營運者金鑰付費,以每 IP 每日 + 全站每日總量雙限
# 保護荷包。只做短音檔/短片段(≤ TRIAL_MINUTES 分鐘)。
# =============================================================
_trial_ip: dict = {}                       # ip -> {"date": date, "sec": used}
_trial_global = {"date": None, "sec": 0}
_trial_lock = threading.Lock()
_trial_slots = threading.Semaphore(1)      # 試用獨立併發槽,不搶付費的 _audio_slots


def _trial_limits():
    try:
        per_ip = max(1, int(os.environ.get("TRIAL_MINUTES", "10"))) * 60
    except ValueError:
        per_ip = 600
    try:
        total = int(os.environ.get("TRIAL_DAILY_TOTAL_MINUTES", "300")) * 60  # 0=不限
    except ValueError:
        total = 300 * 60
    return per_ip, total


def _trial_today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _trial_db_enabled() -> bool:
    """有設定 Supabase service_role 就用資料庫持久化(重啟不歸零、多實例一致)"""
    _, supabase_url, service_key = _paid_env()
    return bool(supabase_url and service_key)


def _rpc1(fn: str, payload: dict):
    """呼叫 RPC 並取回單列結果(PostgREST 回 list)"""
    res = _sb_rpc(fn, payload)
    if isinstance(res, list):
        return res[0] if res else None
    return res


# ---- 記憶體後備(未設定 Supabase 時,例如純 byok 部署) ----
def _trial_used(key: str, today: str) -> int:
    rec = _trial_ip.get(key)
    return rec["sec"] if rec and rec.get("date") == today else 0


def _trial_remaining(keys) -> int:
    """今日剩餘秒數:取所有計量鍵(IP、裝置)中最小的剩餘"""
    if isinstance(keys, str):
        keys = [keys]
    per_key, _ = _trial_limits()
    if _trial_db_enabled():
        try:
            v = _rpc1("trial_remaining", {"p_keys": keys, "p_per_key_limit": per_key})
            return int(v if v is not None else per_key)
        except Exception:
            pass  # 資料庫暫時不可用 → 退回記憶體估算(從寬,避免擋住使用者)
    today = _trial_today()
    with _trial_lock:
        used = max((_trial_used(k, today) for k in keys), default=0)
    return max(0, per_key - used)


def _trial_reserve(keys, cost: int):
    """檢查每鍵(IP/裝置)與全站每日剩餘;足夠則對所有鍵記入用量。不足回 (False, 原因)。
    有 Supabase 時走資料庫原子 RPC(持久化);否則用行程記憶體。"""
    if isinstance(keys, str):
        keys = [keys]
    per_key, total = _trial_limits()
    if _trial_db_enabled():
        try:
            row = _rpc1("trial_reserve", {"p_keys": keys, "p_cost": cost,
                                          "p_per_key_limit": per_key, "p_total_limit": total})
        except Exception:
            row = "db_error"   # RPC 尚未建立(migration 未跑)或暫時不可用 → 退回記憶體
        if row != "db_error":
            if row and row.get("ok"):
                return True, None
            return False, (row or {}).get("reason") or "ip"
    today = _trial_today()
    with _trial_lock:
        if _trial_global.get("date") != today:
            _trial_global["date"] = today
            _trial_global["sec"] = 0
            for k in [k for k, v in _trial_ip.items() if v.get("date") != today]:
                del _trial_ip[k]
        for k in keys:
            if _trial_used(k, today) + cost > per_key:
                return False, "ip"
        if total > 0 and _trial_global["sec"] + cost > total:
            return False, "global"
        for k in keys:
            _trial_ip[k] = {"date": today, "sec": _trial_used(k, today) + cost}
        _trial_global["sec"] += cost
    return True, None


def _trial_refund(keys, sec: int) -> None:
    """轉錄失敗時退回已扣的試用秒數(不低於 0)"""
    if sec <= 0:
        return
    if isinstance(keys, str):
        keys = [keys]
    if _trial_db_enabled():
        try:
            _sb_rpc("trial_refund", {"p_keys": keys, "p_cost": sec})
            return
        except Exception:
            pass
    today = _trial_today()
    with _trial_lock:
        for k in keys:
            rec = _trial_ip.get(k)
            if rec and rec.get("date") == today:
                rec["sec"] = max(0, rec["sec"] - sec)
        if _trial_global.get("date") == today:
            _trial_global["sec"] = max(0, _trial_global["sec"] - sec)


_DEVICE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _trial_keys(ip: str, device_id: str | None) -> list:
    """試用額度的計量鍵:IP 一定算;裝置 ID(前端隨機 UUID)合法才多算一鍵。
    不合法的裝置 ID 不可當成身分(等同未提供)。"""
    keys = [ip]
    if device_id and _DEVICE_ID_RE.match(device_id):
        keys.append("dev:" + device_id.lower())
    return keys


def _content_length_precheck(request: Request) -> None:
    """在讀取 multipart 前先看 Content-Length,超限直接 413(仍以串流累計為準)"""
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_FILE_BYTES + 1024 * 1024:   # multipart 邊界的緩衝
                raise HTTPException(413, "檔案超過 100 MB 上限")
        except ValueError:
            pass


@app.post("/api/trial")
def api_trial(
    request: Request,
    file: UploadFile | None = File(None),
    youtube_url: str | None = Form(None),
    start: float | None = Form(None),
    end: float | None = Form(None),
    correct: bool = Form(True),
    timestamps: bool = Form(False),
    remove_fillers: bool = Form(False),
    speakers: bool = Form(False),
    device_id: str | None = Form(None),
):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(503, "免費試用暫時未啟用")
    _content_length_precheck(request)
    rate_check("audio", request)
    ip = _trial_keys(client_ip(request), device_id)
    per_ip, _ = _trial_limits()
    remain = _trial_remaining(ip)
    if remain <= 0:
        raise HTTPException(429, f"今日免費試用({per_ip // 60} 分鐘)已用完,登入儲值即可繼續使用")

    # 試用用獨立併發槽(最多 1),不佔用付費轉錄共用的 _audio_slots
    if not _trial_slots.acquire(blocking=False):
        raise HTTPException(429, "免費試用忙碌中,請稍候一分鐘再試")
    tmpdir = tempfile.mkdtemp(prefix="ytpaid-")
    src = os.path.join(tmpdir, "src")
    reserved = 0
    try:
        if youtube_url:
            check_url(youtube_url)
            # 試用只截取「今日剩餘額度」窗:未給或給太長的終點,夾到 起點+剩餘,
            # 避免家用中繼抓回註定會被拒的長片段
            s = start if (start and start > 0) else 0
            if end is not None and end <= s:
                raise HTTPException(400, "終點必須大於起點")
            cap_end = s + remain
            if end is None or end > cap_end:
                end = cap_end
            start = s
            _relay_fetch_audio(youtube_url, start, end, src)
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
        else:
            raise HTTPException(400, "請提供音檔或 YouTube 網址")

        duration = _ffprobe_seconds(src)
        if duration <= 0:
            raise HTTPException(400, "音檔長度為 0 或無法辨識")
        if duration > per_ip:
            raise HTTPException(400, f"免費試用僅限 {per_ip // 60} 分鐘內,請截取較短片段或登入使用")
        if duration > remain:
            raise HTTPException(429, f"今日剩餘免費額度約 {remain // 60} 分鐘,不足以轉錄這段音訊({int(duration) // 60 + 1} 分鐘內),請截短或登入儲值使用")

        ok, why = _trial_reserve(ip, int(math.ceil(duration)))
        if not ok:
            if why == "global":
                raise HTTPException(429, "今日免費試用總量已滿,請稍後或登入儲值使用")
            raise HTTPException(429, f"今日免費試用({per_ip // 60} 分鐘)已用完,登入儲值即可繼續使用")
        reserved = int(math.ceil(duration))

        m4a = os.path.join(tmpdir, "audio.m4a")
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
                 "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 "-movflags", "+faststart", m4a],
                check=True, timeout=600, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"音訊轉檔失敗:{(e.stderr or b'').decode(errors='ignore')[:200]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "音訊轉檔逾時")

        if timestamps:
            # YouTube 截取時,時間軸以原片時間為準(加 start 偏移);上傳檔則從 0 起算
            offset = float(start or 0) if youtube_url else 0.0
            raw_text = _segments_to_text(_whisper_transcribe_segments(m4a, api_key), offset)
        else:
            raw_text = _whisper_transcribe(m4a, api_key)
        text, corrected = raw_text, False
        if correct and raw_text:
            try:
                text = _gpt_correct(raw_text, api_key, remove_fillers, speakers)
                corrected = True
            except Exception:
                pass
        resp = {"text": text, "raw_text": raw_text, "corrected": corrected,
                "trial_remaining_minutes": _trial_remaining(ip) // 60}
        reserved = 0   # 成功,額度確定消耗;失敗路徑由 finally 退還
        return resp
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"轉錄失敗:{str(e)[:200]}")
    finally:
        if reserved:
            _trial_refund(ip, reserved)
        _trial_slots.release()
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/api/trial/status")
def api_trial_status(request: Request, device_id: str | None = Query(None)):
    per_ip, _ = _trial_limits()
    enabled = bool(os.environ.get("OPENAI_API_KEY"))
    keys = _trial_keys(client_ip(request), device_id)
    return {"enabled": enabled, "trial_minutes": per_ip // 60,
            "remaining_minutes": _trial_remaining(keys) // 60}
