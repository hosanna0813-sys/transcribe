# 家用電腦中繼站架設指南

讓家裡一台常開的電腦負責抓取 YouTube 音訊。家用網路是「住宅 IP」,YouTube 天生信任它——**不需要 Google 帳號、不需要 cookies**,通過率遠高於雲端機房。網頁會優先連家用中繼站,家裡電腦關機時自動改用 Render 備援。

跑的程式與 Render 上完全相同(`server/main.py`),同樣具備:防濫用速率限制、只允許本工具網頁呼叫(CORS)、音訊用完即刪不留檔。

---

## Windows 版步驟

### 一、安裝環境(只做一次)

1. **Python**:到 https://www.python.org/downloads/ 下載安裝,安裝時**務必勾選「Add python.exe to PATH」**
2. **ffmpeg**:按 `Win` 鍵搜尋「cmd」開啟命令提示字元,執行:
   ```
   winget install Gyan.FFmpeg
   ```
   裝完**關閉並重開**命令提示字元視窗
3. **下載本專案**:到 https://github.com/hosanna0813-sys/transcribe → 綠色 **Code** 按鈕 → **Download ZIP** → 解壓縮到好找的位置(例如 `C:\transcribe`)

### 二、啟動服務

雙擊 `server\home\start_home.bat`。第一次會自動安裝依賴(幾分鐘),看到 `Uvicorn running on http://0.0.0.0:8787` 就是成功。**這個視窗要保持開著。**

### 三、架隧道(讓網頁連得到你家)

1. 到 https://ngrok.com 註冊免費帳號
2. 登入後到 **Setup & Installation** 下載 Windows 版 ngrok,解壓縮
3. 依頁面指示執行一次 `ngrok config add-authtoken 你的token`
4. 到儀表板 **Domains** 頁面,免費帳號可建立 **1 個固定網域**(長得像 `xxxx.ngrok-free.app`),建立它
5. 執行(把網域換成你的):
   ```
   ngrok http 8787 --domain=xxxx.ngrok-free.app
   ```
   **這個視窗也要保持開著。**

### 四、回報網址

把 `https://xxxx.ngrok-free.app` 這個網址告訴開發者填入網頁(或自行修改 `index.html` 開頭的 `DEFAULT_YT_SERVERS`,把它放在清單第一位)。

### 五、常開設定

- 「設定 → 系統 → 電源」把「睡眠」設為**永不**(螢幕可以關)
- 重開機後:重新雙擊 `start_home.bat` + 重下 ngrok 指令即可

---

## Mac 版步驟

```bash
# 一、安裝環境(只做一次;需先裝 Homebrew:https://brew.sh)
brew install python ffmpeg ngrok

# 二、下載專案並啟動服務(視窗保持開著)
# 先從 GitHub 下載 ZIP 解壓,或 git clone,然後:
cd transcribe/server/home
sh start_home.sh

# 三、另開一個終端機視窗,架隧道(先到 ngrok.com 註冊、設 authtoken、建固定網域)
ngrok config add-authtoken 你的token
ngrok http 8787 --domain=xxxx.ngrok-free.app
```

其後同 Windows 版步驟四、五(「系統設定 → 鎖定畫面/能源」關閉睡眠)。

---

## 常見問題

- **家裡電腦關機會怎樣?** 網頁自動改用 Render 備援(成功率較低);開回來就恢復。
- **會佔多少資源?** 平時幾乎為零,抓取時短暫使用網路與 CPU;音訊皆壓縮後傳出(1 小時約 14 MB)。
- **安全嗎?** 服務只做 YouTube 音訊抓取,有每 IP 每小時次數限制,且只接受本工具網頁的瀏覽器請求;不碰你電腦上的任何檔案。
- **ngrok 免費版夠用嗎?** 夠。固定網域 1 個、流量額度對音訊傳輸綽綽有餘。
