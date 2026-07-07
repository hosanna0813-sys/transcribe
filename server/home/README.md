# 家用電腦中繼站架設指南

讓家裡一台常開的電腦負責抓取 YouTube 音訊。家用網路是「住宅 IP」,YouTube 天生信任它——**不需要 Google 帳號、不需要 cookies**,通過率遠高於雲端機房。網頁會優先連家用中繼站,家裡電腦關機時自動改用 Render 備援。

跑的程式與 Render 上完全相同(`server/main.py`),同樣具備:防濫用速率限制、只允許本工具網頁呼叫(CORS)、音訊用完即刪不留檔。

---

## 方式一:Docker 沙箱版(推薦)

程式與 ngrok 隧道都關在 Docker 容器裡跑:**不直接接觸你的系統**,即使套件未來出漏洞也摸不到你的檔案;電腦重開機後容器自動復活,維護最省事。

### 一、安裝 Docker Desktop(只做一次)

1. 到 https://www.docker.com/products/docker-desktop/ 下載對應版本(Windows / Mac)
2. 用預設選項安裝(Windows 若提示啟用 WSL 2,照提示按確定即可,可能需要重開機)
3. 開啟 Docker Desktop,左下角變綠色 running 就緒
4. 建議到 Docker Desktop 的 Settings → General 勾選「**Start Docker Desktop when you sign in**」(開機自動啟動)

### 二、註冊 ngrok(只做一次)

1. 到 https://ngrok.com 免費註冊
2. 儀表板「**Your Authtoken**」頁面 → 複製 token
3. 儀表板「**Domains**」頁面 → 建立 1 個免費**固定網域**(長得像 `xxxx.ngrok-free.app`)

### 三、下載專案並設定

1. 到 https://github.com/hosanna0813-sys/transcribe → 綠色 **Code** → **Download ZIP** → 解壓縮(例如放 `C:\transcribe`)
2. 進入 `server\home` 資料夾,把 `.env.example` **複製一份改名為 `.env`**,用記事本打開,填入上一步的 authtoken 與網域,存檔

### 四、啟動(之後重開機都不用再做)

開啟終端機(Windows:在 `server\home` 資料夾的網址列輸入 `cmd` 按 Enter;Mac:終端機 `cd` 到該資料夾),執行:

```
docker compose up -d
```

第一次會自動建置(約 5–10 分鐘),之後幾秒就好。驗證:用瀏覽器打開 `https://你的網域/healthz`,看到 `{"ok":true,...}` 就是成功。

**完成後把你的網域網址回報給開發者**填入網頁,或自行修改 `index.html` 開頭的 `DEFAULT_YT_SERVERS` 清單首位。

### 日常維護

- 電腦重開機:Docker Desktop 自動啟動 → 容器自動復活,**什麼都不用做**
- 想停止:同資料夾執行 `docker compose down`
- 更新程式:重新下載 ZIP 覆蓋後執行 `docker compose up -d --build`
- 電源設定:「睡眠」設為永不(螢幕可以關)

---

## 方式二:直接安裝版(不用 Docker)

<details>
<summary>展開查看(Python + ffmpeg + ngrok 手動安裝)</summary>

### Windows

1. **Python**:https://www.python.org/downloads/ 下載安裝,務必勾選「Add python.exe to PATH」
2. **ffmpeg**:命令提示字元執行 `winget install Gyan.FFmpeg`,裝完重開視窗
3. 下載本 repo ZIP 解壓,雙擊 `server\home\start_home.bat`(視窗保持開著)
4. 下載 ngrok:https://ngrok.com/download,設定 authtoken 後執行:
   `ngrok http 8787 --domain=你的固定網域`(視窗保持開著)
5. 重開機後需重複步驟 3–4

### Mac

```bash
brew install python ffmpeg ngrok
cd transcribe/server/home && sh start_home.sh
# 另開視窗:
ngrok config add-authtoken 你的token
ngrok http 8787 --domain=你的固定網域
```

</details>

---

## 常見問題

- **家裡電腦關機會怎樣?** 網頁自動改用 Render 備援(成功率較低);開回來就恢復。
- **會佔多少資源?** 平時幾乎為零,抓取時短暫使用網路與 CPU;音訊皆壓縮後傳出(1 小時約 14 MB)。
- **安全嗎?** 服務只做 YouTube 音訊抓取,有每 IP 每小時次數限制、只認 YouTube 網址、不碰你電腦上的檔案;Docker 版更把程式整個關在沙箱裡,且不在主機上開放任何連接埠(只有 ngrok 容器經內部網路能連到它)。
- **ngrok 免費版夠用嗎?** 夠。固定網域 1 個、流量額度對音訊傳輸綽綽有餘。
