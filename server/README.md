# YouTube 音訊擷取服務(Render 部署說明)

這是「語音轉逐字稿」工具的選用後端,只負責一件事:把 YouTube 影片的聲音抓下來、壓成最小的音訊檔回傳給網頁,網頁再用**你自己的** OpenAI API Key 進行 Whisper 轉錄。

**隱私與空間**:伺服器不保存任何檔案(暫存檔用完立即刪除)、不經手你的 API Key;音訊一律壓成 16kHz 單聲道 32kbps(1 小時約 14 MB),且只下載你指定的起訖片段,不會佔用 Render 空間。

## 部署步驟(約 5 分鐘,免費方案即可)

1. 打開 [Render Dashboard](https://dashboard.render.com/),點右上 **New +** → **Blueprint**
2. 連結你的 GitHub 帳號,選擇 **transcribe** 這個 repo
3. Render 會自動讀取 repo 根目錄的 `render.yaml`,顯示要建立的服務 `transcribe-yt`,點 **Apply**
4. 等待部署完成(第一次約 3–5 分鐘),完成後服務頁面上方會顯示網址,長得像:
   `https://transcribe-yt-xxxx.onrender.com`
5. 複製這個網址,回到逐字稿工具 → 展開「進階選項」→ 貼到「**YouTube 伺服器網址**」欄位(會自動記住)

完成!之後在工具的步驟二貼上 YouTube 網址即可轉錄。

## 注意事項

- **免費方案會休眠**:閒置 15 分鐘後服務會睡著,下一次使用的第一個請求需要等 30–60 秒喚醒,工具會顯示提示,稍等或再按一次即可。
- **YouTube 偶爾會擋雲端伺服器**:若出現「下載音訊失敗:Sign in to confirm…」之類的訊息,表示 YouTube 暫時要求該伺服器驗證身分。通常過一段時間會恢復;若長期發生,可在 Render 服務的環境變數加入 cookies 檔(進階做法,需要時再詢問)。
- **限制**:整部影片上限 3 小時;截取片段上限 2 小時;單一請求音訊上限 100 MB。

## API(前端自動使用,一般不需手動呼叫)

| 路徑 | 說明 |
|---|---|
| `GET /info?url=<YT網址>` | 回傳影片標題與長度 |
| `GET /audio?url=<YT網址>&start=<秒>&end=<秒>` | 回傳截取後的壓縮音訊(start/end 可省略) |
| `GET /healthz` | 健康檢查 |
