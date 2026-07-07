#!/bin/sh
# 語音轉逐字稿 - 家用中繼站(Mac/Linux)
set -e
cd "$(dirname "$0")/.."

command -v python3 >/dev/null 2>&1 || { echo "[錯誤] 找不到 python3,請先安裝(Mac:brew install python)"; exit 1; }
command -v ffmpeg  >/dev/null 2>&1 || { echo "[錯誤] 找不到 ffmpeg,請先安裝(Mac:brew install ffmpeg)"; exit 1; }

if [ ! -d .venv ]; then
  echo "[第一次啟動] 建立虛擬環境並安裝依賴,約需數分鐘……"
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "============================================================"
echo " 服務已啟動:http://localhost:8787"
echo " 請另開終端機執行:ngrok http 8787 --domain=你的固定網域"
echo " 此視窗請保持開啟;按 Ctrl+C 可停止服務。"
echo "============================================================"
echo ""
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8787
