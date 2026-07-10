"""綠界 ECPay 金流:方案、環境判斷、CheckMacValue 簽章。

只含「純運算 + 設定」:AIO 端點、儲值方案、簽章演算法、production fail-closed。
路由與資料庫入帳仍在 main.py(呼叫本模組)。環境變數於呼叫時讀取,便於測試覆寫。
"""
import hashlib
import os
import urllib.parse

from fastapi import HTTPException

# 綠界公開測試帳號(官方文件提供,任何人可用於測試環境)
_ECPAY_TEST = {"mid": "2000132", "key": "5294y06JbISpM5x9", "iv": "v77hoKGq4kWxNNIS"}
AIO_URLS = {
    "stage": "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5",
    "production": "https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5",
}

# 儲值方案(價格由營運者自行調整;1 分鐘 = 60 credits)
PAY_PACKAGES = [
    {"id": "p100", "amount_twd": 100, "minutes": 60},
    {"id": "p300", "amount_twd": 300, "minutes": 200},
    {"id": "p500", "amount_twd": 500, "minutes": 360},
]


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() == "production"


def ecpay_conf():
    """回傳 (mid, key, iv, env)。production 缺正式金鑰 → fail closed(不退回測試金鑰)。"""
    mid = os.environ.get("ECPAY_MERCHANT_ID", "")
    key = os.environ.get("ECPAY_HASH_KEY", "")
    iv = os.environ.get("ECPAY_HASH_IV", "")
    env = os.environ.get("ECPAY_ENV", "stage")
    if not (mid and key and iv):
        if _is_production():
            raise HTTPException(503, "金流尚未設定完成,暫停儲值")
        mid, key, iv = _ECPAY_TEST["mid"], _ECPAY_TEST["key"], _ECPAY_TEST["iv"]
    return mid, key, iv, ("production" if env == "production" else "stage")


def ecpay_checkmac(params: dict) -> str:
    """依綠界規格計算 CheckMacValue(EncryptType=1,SHA256)"""
    _, key, iv, _ = ecpay_conf()
    items = sorted(((k, v) for k, v in params.items() if k != "CheckMacValue"),
                   key=lambda x: x[0].lower())
    raw = "HashKey=" + key + "&" + "&".join(f"{k}={v}" for k, v in items) + "&HashIV=" + iv
    enc = urllib.parse.quote_plus(raw).lower()
    for a, b in [("%2d", "-"), ("%5f", "_"), ("%2e", "."), ("%21", "!"),
                 ("%2a", "*"), ("%28", "("), ("%29", ")"), ("%20", "+")]:
        enc = enc.replace(a, b)
    return hashlib.sha256(enc.encode()).hexdigest().upper()
