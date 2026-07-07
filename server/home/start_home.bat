@echo off
chcp 65001 >nul
title 語音轉逐字稿 - 家用中繼站
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 Python。請到 https://www.python.org/downloads/ 安裝,
  echo        安裝時務必勾選「Add python.exe to PATH」,然後重開此視窗。
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 ffmpeg。請先在命令提示字元執行:
  echo        winget install Gyan.FFmpeg
  echo        安裝完成後關閉並重開此視窗。
  pause
  exit /b 1
)

if not exist .venv (
  echo [第一次啟動] 建立 Python 虛擬環境並安裝依賴,約需數分鐘……
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

echo.
echo ============================================================
echo  服務已啟動:http://localhost:8787
echo  請另開視窗執行:ngrok http 8787 --domain=你的固定網域
echo  此視窗請保持開啟;按 Ctrl+C 可停止服務。
echo ============================================================
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8787
pause
