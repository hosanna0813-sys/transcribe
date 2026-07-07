# YouTube 音訊擷取服務(站長部署一次,訪客零設定)

這是「語音轉逐字稿」工具的後端,只負責一件事:把 YouTube 影片的聲音抓下來、壓成最小的音訊檔回傳給網頁,網頁再用**使用者自己的** OpenAI API Key 進行 Whisper 轉錄。

**站長只需部署一次**,把服務網址填入 `index.html` 開頭的 `DEFAULT_YT_SERVER` 常數,之後所有打開網頁的人都能直接使用,不需任何設定。

## 部署步驟(約 5 分鐘,免費方案即可)

1. 打開 [Render Dashboard](https://dashboard.render.com/),點右上 **New +** → **Blueprint**
2. 連結你的 GitHub 帳號,選擇 **transcribe** 這個 repo
3. Render 會自動讀取 repo 根目錄的 `render.yaml`,顯示要建立的服務 `transcribe-yt`,點 **Apply**
4. 等待部署完成(第一次約 3–5 分鐘),完成後服務頁面上方會顯示網址,長得像:
   `https://transcribe-yt-xxxx.onrender.com`
5. 把這個網址填入 `index.html` 裡的 `DEFAULT_YT_SERVER` 常數(在 `<script>` 開頭處),例如:
   ```js
   var DEFAULT_YT_SERVER='https://transcribe-yt-xxxx.onrender.com';
   ```
   (原始寫法是讀取 `window.__DEFAULT_YT_SERVER` 再退回空字串,只要把空字串換成你的網址即可)

完成!所有訪客打開網頁即可直接貼 YouTube 網址轉錄。進階使用者仍可在「進階選項」填入自架伺服器覆蓋內建值。

## 隱私、空間與防濫用

- **零保存**:暫存檔用完立即刪除(成功或失敗都清理),伺服器不保存任何音訊,也不經手任何人的 API Key
- **檔案極小**:音訊一律壓成 16kHz 單聲道 32kbps(1 小時約 14 MB),並優先只下載指定的起訖片段
- **防濫用**(服務是公開的,已內建保護):
  - 每個 IP 每小時最多查詢 30 次影片資訊、擷取 10 次音訊
  - 同時最多處理 2 個下載工作,滿載回覆「請稍候再試」
  - 只接受來自本工具網頁(hosanna0813-sys.github.io)的瀏覽器請求(CORS)
- **限制**:整部影片上限 3 小時;截取片段上限 2 小時;單一請求音訊上限 100 MB

## 注意事項

- **免費方案會休眠**:閒置 15 分鐘後服務會睡著,下一次使用的第一個請求需要等 30–60 秒喚醒,工具會顯示提示,稍等或再按一次即可。
- **YouTube 偶爾會擋雲端伺服器**:若出現「下載音訊失敗:Sign in to confirm…」之類的訊息,表示 YouTube 暫時要求該伺服器驗證身分。通常過一段時間會恢復;若長期發生,可在 Render 服務加入 cookies 檔(進階做法,需要時再詢問)。
- 片段下載被拒時會自動改抓完整音訊、在伺服器本地截取後回傳,使用者無感。

## API(前端自動使用,一般不需手動呼叫)

| 路徑 | 說明 |
|---|---|
| `GET /info?url=<YT網址>` | 回傳影片標題與長度 |
| `GET /audio?url=<YT網址>&start=<秒>&end=<秒>` | 回傳截取後的壓縮音訊(start/end 可省略) |
| `GET /healthz` | 健康檢查 |
