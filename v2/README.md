# 計費版 v2 — 階段一設定指南(帳號登入 + 顯示剩餘分鐘)

這是「語音轉逐字稿」計費版的第一階段。目標很單純:**使用者能登入、能看到自己的
剩餘分鐘數**。這一階段還沒有真正的轉錄、扣點、加值功能——那些會在後續階段陸續加上。

- 舊版(免費、自帶 OpenAI Key)完全不受影響,仍在原網址運作。
- 新版位於 `v2/` 資料夾,發布後網址是 `<你的 GitHub Pages 網址>/v2/`。

---

## 一次性設定步驟

### 1. 建立 Supabase 專案
1. 到 <https://supabase.com> 註冊並登入(免費方案即可)。
2. 建立一個新的 Project(記下你設定的資料庫密碼)。
3. 專案建立完成後,到左側 **Project Settings → API**,記下兩個值:
   - **Project URL**(形如 `https://xxxxxxxx.supabase.co`)
   - **anon public** key(一長串 `eyJ...`)
   > ⚠️ 同頁還有一個 **service_role** key——那是**後端專用的機密金鑰**,
   > 這一階段用不到,**千萬不要**填進前端網頁或給任何人。

### 2. 啟用 Email 登入
1. 到左側 **Authentication → Providers**。
2. 確認 **Email** 是啟用狀態(預設啟用)。本階段用「Email 魔術連結」登入,
   不需密碼、也不需設定 Google OAuth。
3.(建議)到 **Authentication → URL Configuration**,把 **Site URL** 設成你的
   `.../v2/` 網址,並在 **Redirect URLs** 加入同一個網址,登入連結才會正確導回。

> ⚠️ **網站改到根網域後必做**:登入導回網址是「當下開啟的頁面」,所以 Supabase 的
> **Redirect URLs** 必須把新網址也加進去,否則從新網址登入會導錯或失敗。
> 到 **Authentication → URL Configuration → Redirect URLs** 新增:
> `https://hosanna0813-sys.github.io/v2/`
> (保留原本的 `.../transcribe/v2/`,兩個網址都能登入。)也建議把 **Site URL**
> 改成 `https://hosanna0813-sys.github.io/v2/`。

### 3. 建立資料表與安全規則
1. 到左側 **SQL Editor → New query**。
2. 把本資料夾的 `schema.sql` 整份貼上,按 **Run**。
3. 成功後會建立五張資料表(profiles、credit_balances、credit_transactions、
   usage_logs、payments),並套用 RLS 安全規則(前端只能讀自己的資料、不能改)。

### 4. 把 Supabase 資訊填進網頁
開啟 `v2/index.html`,找到最上方這段,填入第 1 步記下的兩個值:
```js
var SUPABASE_CONFIG = {
  url: 'https://xxxxxxxx.supabase.co',   // 你的 Project URL
  anonKey: 'eyJhbGciOi...'               // 你的 anon public key
};
```
> anon key 可以公開放在前端(這是它設計的用途),真正的安全防護在資料庫的 RLS
> 規則——使用者即使拿到 anon key,也只能讀到自己的資料、無法竄改任何餘額。

### 5. 部署
把改好的 `v2/` 資料夾推上 GitHub(合併到 `main`),GitHub Pages 會自動發布。
稍候幾分鐘,打開 `<你的 Pages 網址>/v2/` 即可看到登入頁。

---

## 測試流程

1. 打開 `.../v2/`,輸入你的 Email → 按「寄送登入連結」。
2. 到信箱點擊登入連結,會導回網頁並顯示「已登入」與「剩餘額度 0 分鐘」。
3. 給自己一些測試額度:回到 Supabase **SQL Editor**,執行(把 email 換成你的登入信箱):
   ```sql
   update public.credit_balances
     set remaining_credits = 1200, updated_at = now()
     where user_id = (select id from auth.users where email = 'YOUR_EMAIL@example.com');
   ```
   （1200 credits = 20 分鐘）
4. 回到網頁重新整理,應該看到「剩餘額度 20 分鐘」。

### 驗證安全規則有生效(選做)
在瀏覽器 Console 試著改自己的餘額,應該會被 RLS 擋下、改不動:
```js
// 應回傳錯誤或 0 筆更新,無法把餘額改大
await sb.from('credit_balances').update({remaining_credits: 999999}).eq('user_id', (await sb.auth.getUser()).data.user.id)
```

---

## 常見問題

- **收不到登入信?** 檢查垃圾信匣;Supabase 免費方案內建寄信有每小時寄送上限,
  正式上線建議在 Authentication → Emails 設定自己的 SMTP。
- **點連結沒導回?** 確認第 2 步的 Site URL / Redirect URLs 有設成你的 `.../v2/` 網址。
- **顯示「尚未設定 Supabase」?** 表示 `SUPABASE_CONFIG` 的 url / anonKey 還沒填。

---

# 階段二設定:上傳短音檔 → 轉錄 → 扣點

階段二讓使用者能**上傳短音檔(10 分鐘內),由後端用你的 OpenAI 金鑰轉錄,並依
音檔長度扣除 Credits**。轉錄在 Render 雲端後端執行,你的金鑰只放後端、不進前端。

### A. 建立扣點函式(Supabase)
到 **SQL Editor → New query**,貼上 `v2/schema_phase2.sql` 整份,按 **Run**。
(這會建立 reserve / complete / fail 三個原子扣點函式,只允許後端 service_role 呼叫。)

### B. 在 Render 後台設三個環境變數
到 Render 的這個服務 → **Environment** → 新增(值都不會進 git):
| 變數名 | 值 |
| --- | --- |
| `OPENAI_API_KEY` | 你的 OpenAI 金鑰(`sk-...`),會付所有轉錄費用 |
| `SUPABASE_URL` | `https://ocfyfwcdpzllkmbkldtv.supabase.co`(你的 Project URL) |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API 的 **service_role secret** 金鑰 |
> ⚠️ `service_role` 是**最高權限機密金鑰**,只能放 Render 後台,絕不可貼進前端或 git。

設定後 Render 會自動重新部署(約幾分鐘)。未設定前付費轉錄會回「尚未啟用」,但免費版 YouTube 服務照常。

### C. 測試
1. 到 `.../v2/` 登入(確保有測試額度,不夠就用階段一的 SQL 加值)。
2. 在「上傳音檔轉錄」選一個 10 分鐘內的音檔 → 按「開始轉錄」。
   （Render 免費方案若休眠,第一次會等 30–60 秒喚醒。）
3. 應看到逐字稿結果,且上方「剩餘分鐘」對應減少(1 秒 = 1 credit)。
4. 驗證扣點:到 Supabase Table Editor 看 `credit_transactions`(一筆 deduct)與
   `usage_logs`(status=completed)。

### 常見問題
- **「付費轉錄尚未啟用」** → Render 三個環境變數還沒設好或還在部署。
- **「剩餘額度不足」** → 用階段一的 SQL 加值,或該音檔太長超過額度。
- **一直轉圈很久** → Render 免費方案冷啟動較慢,第一次請耐心等;之後會快。

---

# 管理員加值介面(取代手動下 SQL)

登入後如果你是管理員,帳號頁會多出「**管理員:為帳號加值**」區塊,直接輸入
使用者 Email 與分鐘數即可加值,不用再進 SQL Editor。

### A. 建立加值函式(Supabase)
SQL Editor → 貼上 `v2/schema_phase4.sql` 整份 → **Run**。

### B. 把自己設為管理員(只需一次)
同樣在 SQL Editor 執行(把 email 換成你的登入信箱):
```sql
update public.profiles set role = 'admin', updated_at = now()
  where id = (select id from auth.users where email = 'hosanna0813@gmail.com');
```

### C. 使用
到 `.../v2/` 登入(強制重新整理),帳號頁會出現加值區塊:
1. 填「使用者 Email」(對方需先登入過至少一次,帳號才存在)。
2. 填「加值分鐘數」(例如 60)。
3. (選填)備註,例如「2026-07 儲值 NT$100」,方便日後對帳。
4. 按「加值」→ 成功會顯示對方目前剩餘分鐘。

### 安全與對帳
- 加值端點在後端**再次確認呼叫者是管理員**,前端動任何手腳都無效;非管理員看不到也不能加值。
- 每次加值都寫入 `credit_transactions`(type=add),可到 Supabase 對帳。
- 有冪等保護:連點或網路重試不會重複加值。

---

# 階段三:YouTube 來源 + 長音檔(最長 3 小時)

付費版現在支援 **YouTube 網址** 與 **長音檔(最長 3 小時)**。長音檔會自動分段轉錄,
送出後前端顯示進度、可留在頁面等候。YouTube 下載走你的家用中繼(住宅 IP,較不會被擋)。

### A. 建立所需資料庫調整(Supabase)
SQL Editor → 貼上 `v2/schema_phase3.sql` 整份 → **Run**。
(新增逐字稿暫存欄位、擴充結算函式、新增取結果函式。)

### B-0. 保護家用中繼(強烈建議,一次設定)
家用中繼的 ngrok 網址一旦外流,任何人都能用你家頻寬抓片。已內建 HMAC 簽章驗證:
1. 產生一串長隨機密鑰(電腦上執行 `openssl rand -hex 32`,或隨便打 40+ 個亂字元)。
2. 家用機:`server/home/.env` 填 `RELAY_SHARED_SECRET=該密鑰`,重新 `docker compose up -d --build`。
   之後**未帶有效簽章的請求一律 403**(包含直接打 ngrok 網址的人)。
3. Render:**Environment** 加同一個 `RELAY_SHARED_SECRET=該密鑰`(後端對中繼的請求會自動簽章)。
4. 驗收:瀏覽器直接開 `https://你的ngrok網域/info?url=...` 應得到 403;
   但網站的 YouTube 轉錄照常可用。

### B. Render 新增一個環境變數
到 Render 服務 → **Environment** → 新增:
| 變數名 | 值 |
| --- | --- |
| `HOME_RELAY_URL` | 你的家用中繼 ngrok 網址,例如 `https://woof-stand-rectify.ngrok-free.dev` |
> 未設定此變數時,YouTube 來源會顯示「尚未啟用」,但**上傳音檔仍可用**。
> YouTube 轉錄時你的**家用電腦要開著**(由它負責下載,雲端只負責轉錄)。

### C. 測試
到 `.../v2/`(強制重新整理)登入後,「轉錄」區塊可以:
1. **上傳檔案**(音檔或影片,最長 3 小時),或
2. **貼 YouTube 網址**(可選填起訖時間只轉某段)。
按「開始轉錄」→ 顯示進度(轉錄中 X%)→ 完成顯示逐字稿、扣除對應分鐘。

### 說明
- 長音檔自動切成約 9 分鐘一段分批轉錄,逐字稿每段前有 `[時:分:秒]` 時間標記。
- 送出後可留在頁面看進度;若不小心重新整理,回到頁面會自動接回進行中的任務。
- 任務失敗(含伺服器重啟中斷)會**自動退款**,不吃額度。
- 暫存音檔任務結束即刪,並有每 24 小時的清理兜底(零保存)。

---

# GPT 校正(修正錯字、標點)

轉錄區塊有一個「**轉錄後用 AI 校正逐字稿**」勾選框(預設開)。勾選時,轉錄完成後
會用便宜的 AI 模型把逐字稿的錯字、同音誤植、標點修正一遍(不改內容、保留時間標記),
品質接近原版。**免額外設定、也不額外收費**(校正成本約為轉錄的 5%,已內含)。

- 校正萬一失敗,會自動退回未校正的原始逐字稿,不會讓整筆任務失敗、也不影響扣點。
- (選用)想換校正模型,可在 Render 加環境變數 `CORRECTION_MODEL`(預設 `gpt-4o-mini`);
  若改用較貴的模型,建議自行評估是否要對校正加收費用。

---

## 使用紀錄(已完成)

帳號頁有「使用紀錄」區塊,列出最近 20 筆轉錄(日期、來源、長度、花費/免費、狀態),
由前端直接以 RLS 讀取自己的紀錄(不需後端,Render 休眠也能看)。每筆可「刪除紀錄內容」
(去內容化:清空檔名/網址,帳務欄位保留),透過只允許本人操作的安全函式。

**設定**:Supabase SQL Editor 跑 `v2/schema_history.sql`(建立刪除函式)。列表本身無需設定。

---

# 階段六:帳務修正 + 試用持久化(建議盡快套用)

SQL Editor → 貼上 `v2/schema_phase6.sql` 整份 → **Run**(可重複執行)。這份會:
- **修正一個帳務 bug**:先前結算時若實際秒數比預留少,退差額會把「免費額度退成付費
  credits」、且免費用量沒退回。改為**按來源分別退回**(免費退免費、付費退付費),
  並在 `usage_logs` 記 `charged_free`/`charged_paid`。
- 補 **CHECK 約束**(role、餘額非負、status、source、付款金額)與索引。
- **免費試用改資料庫持久化**:新增 `trial_usage`/`trial_global` 兩表與 RPC。
  設好 `SUPABASE_URL`+`SUPABASE_SERVICE_ROLE_KEY` 後,首頁試用額度即改由資料庫計量
  ——**伺服器重啟不再歸零、多實例一致**;未設定 Supabase 時自動退回記憶體模式(byok 部署)。
- **逐字稿結果 TTL**:完成的逐字稿預設 24 小時後由 watchdog 自動去內容化(帳務保留)。
  想改天數,可在 Supabase 執行 `alter database postgres set app.result_ttl_hours = 48;`。

套用後無需改前端;後端偵測到 Supabase 就自動改用資料庫試用計量。

---

# 階段七:成本統計(選用,建議套用以利對帳)

SQL Editor → 貼上 `v2/schema_phase7.sql` 整份 → **Run**(可重複)。這份會:
- 擴充 `complete_transcription`,把每筆付費任務的 **GPT 校正 tokens** 與**費率快照**
  寫進 `usage_logs`(`prompt_tokens`/`completion_tokens`/`estimated_openai_cost_usd`/
  `applied_pricing`);Whisper 成本依實際秒數計。
- 新增 `job_cost_summary` 對帳檢視(每筆任務的成本與收費)。

**價格設定(集中於後端,環境變數可覆蓋)**:預設 Whisper US$0.006/分、gpt-4o-mini
輸入 US$0.15/百萬 tokens、輸出 US$0.60/百萬。要調整,在 Render 設(例):
`PRICE_WHISPER_PER_MIN`、`PRICE_GPT_4O_MINI_IN_PER_1M`、`PRICE_GPT_4O_MINI_OUT_PER_1M`。
> 沒拿到 GPT usage 時,成本明細會標記 `tokens_known=false`(即該筆校正成本為估算)。

> 後端程式已把價格(`pricing.py`)、金流簽章(`ecpay.py`)、中繼驗證(`relay.py`)
> 拆成獨立模組;Dockerfile 已一併複製,無需額外設定。

---

# 階段八:背景任務佇列 + 就地 Worker(建議套用)

SQL Editor → 貼上 `v2/schema_phase8.sql` 整份 → **Run**(可重複)。

**為什麼**:原本長音檔任務是「每個請求開一條執行緒」,程序重啟就中斷、且無法重試。
改成**資料庫佇列 + 就地 Worker**:建立任務時只「入列」(狀態 queued),由一條專責
Worker 執行緒認領處理,狀態/心跳/重試/取消全記在資料庫。

> **關於「獨立 Worker」與 Render 免費方案**:Render 免費方案沒有獨立的 Worker/Cron
> 服務(那是付費功能),所以 Worker 以「Web 服務內的專責執行緒」實作 —— 靠資料庫佇列
> 達成重啟續跑、重試與取消,而非另一個行程。日後升級到付費方案要拆成真正獨立的
> Worker 服務,只需把 `_worker_loop` 放到獨立進入點、共用同一份 `schema_phase8` 佇列即可。

這份 migration 會加:任務生命週期欄位(started_at/heartbeat_at/retry_count/…)、
`queued` 狀態,以及 `enqueue_transcription`/`claim_next_job`/`job_heartbeat`/`cancel_job`/
`sweep_jobs`/`job_retry_or_fail` 等 RPC。

**效果**
- **重啟續跑**:任務狀態在資料庫;程序重啟後 Worker 會重新認領未完成的 YouTube 任務
  (上傳檔任務若暫存已隨重啟消失,則失敗退款,請重新上傳)。
- **心跳 + 自動重試**:Worker 每 30 秒更新心跳;心跳逾 3 分鐘視為死亡 → 退回佇列重試
  (最多 2 次),逾上限才失敗退款。
- **使用者取消**:計費版轉錄進度區多了「取消任務」鈕(打 `POST /api/jobs/{id}/cancel`),
  取消即退款。
- **結果 TTL**:完成的逐字稿 24 小時後自動去內容化(帳務保留)。
- 併發:預設同時 1 筆(免費方案單實例);可用 Render 環境變數 `WORKER_CONCURRENCY` 調整。

套用後無需其他設定;短音檔即時上傳(`/api/transcribe`)維持原本同步流程不變。

# 金流:綠界 ECPay 線上儲值

使用者可在帳號頁的「儲值」區塊選方案 → 線上刷卡 / LINE Pay 付款 → 額度自動入帳。
入帳只由後端在「驗證綠界通知簽章成功」後處理,防偽造、防重複。

### A. 建立入帳函式(Supabase)
SQL Editor → 貼上 `v2/schema_pay.sql` 整份 → **Run**。

### B. 先用「測試環境」驗證(不用先申請帳號)
未設定綠界環境變數時,後端會用**綠界公開測試值**。你只要在 Render 設兩個網址即可測:
| 變數名 | 值 |
| --- | --- |
| `PUBLIC_BASE_URL` | 你的後端網址,例 `https://transcribe-yt-j7kv.onrender.com` |
| `SITE_V2_URL` | 你的 v2 網址,例 `https://hosanna0813-sys.github.io/transcribe/v2/` |

到 `.../v2/` 登入 → 點一個儲值方案 → 會跳到綠界**測試**結帳頁 →
用綠界測試信用卡(卡號 `4311-9522-2222-2222`、有效期任意未來、安全碼任意 3 碼)付款 →
幾秒後回到網站,餘額會增加、`payments` 出現一筆 `paid`。

### C. 正式上線(收真的錢)
1. 到綠界 <https://www.ecpay.com.tw> 申請**個人**特約商店,拿到正式 MerchantID / HashKey / HashIV。
2. Render 設定:`ECPAY_MERCHANT_ID`、`ECPAY_HASH_KEY`、`ECPAY_HASH_IV`,並把 `ECPAY_ENV` 設為 `production`。
3. 在綠界後台把「付款完成通知網址(ReturnURL)」相關設定指向你的 `PUBLIC_BASE_URL`。

### 說明
- **儲值方案價格**寫在後端 `server/main.py` 的 `PAY_PACKAGES`(先放佔位價格,你自行調整);
  前端自動顯示。
- 入帳以綠界**伺服器通知**為準(瀏覽器導回只是顯示);同一筆重送不會重複加值。
- 每筆付款都寫入 `payments` 與 `credit_transactions`,可完整對帳。

> ⚠️ 這是真實金錢功能:請務必**先在測試環境完整驗證**(付款→入帳→餘額正確)再切正式。

---

## 接下來(選用)
- 每日成本/營收監控報表;價格改為資料表管理(目前寫在後端常數,改價要改一行 + 重新部署)。

---

# 階段五:每日免費額度 + 防護機制

### A. 建立所需資料庫調整(Supabase)
SQL Editor → 貼上 `v2/schema_phase5.sql` 整份 → **Run**。
(新增免費額度計數欄位、改寫預扣/退款函式以支援免費額度、新增查詢函式。)

### B. 設定每日免費分鐘(Render)
到 Render 服務 → **Environment** → 新增:
| 變數名 | 值 |
| --- | --- |
| `FREE_DAILY_MINUTES` | 每個帳號每天免費試用的分鐘數,例如 `30`。設 `0` 或不設 = 關閉免費試用 |

### 效果
- **每日免費額度**:每個帳號每天有這麼多分鐘免費;轉錄時**優先扣免費額度**,不足才扣付費
  Credits。用不完的免費額度**隔天(台灣時間午夜)重置**、不累積。帳號頁會顯示「今日免費剩餘」。
- **卡住任務自動退款(watchdog)**:每 15 分鐘掃描,任何逾 4 小時仍未完成的任務(通常是
  當機/重啟/掛住造成)自動標記失敗並退款,不吃使用者額度。
- **防濫用**:每個帳號每小時最多建立 40 個任務;搭配「同時只能 1 個任務」與免費每日上限,
  降低濫用空間。

> 免費額度是「用量計數」不是付費點數,因此不進 `credit_transactions`;付費扣款與退款仍
> 完整記帳、可對帳。

---

# 首頁改版:免登入免費試用 + 廣告版位

首頁(根目錄 `index.html`)改成「**免登入、免費試用 10 分鐘**」的落地頁:訪客直接上傳短音檔或
貼 YouTube 網址截取片段就能拿到逐字稿,不用註冊、不用自備金鑰(由**你的 `OPENAI_API_KEY`
付費**)。需要更多的人再點「登入 / 儲值」進入計費版 v2。

### 三個頁面各自的位置
| 網址 | 內容 |
| --- | --- |
| `/`(首頁) | **免費試用**落地頁,免登入、試用 10 分鐘,含廣告版位 |
| `/v2/` | **計費版**:登入、儲值、長音檔(3 小時)、歷史紀錄 |
| `/byok/` | **進階版**:自備 OpenAI Key、免費不限次(即原本的免費版,原封不動搬到這個網址) |

三個頁面互相有連結,不影響彼此。

### 正式網址:根網域(AdSense 驗證需要)
AdSense 只驗證**網域根目錄**,所以另外建立了使用者頁 repo **`hosanna0813-sys.github.io`**,
正式網址為 <https://hosanna0813-sys.github.io/>(首頁 / `v2/` / `byok/` / `privacy/` / `ads.txt`)。
- **本 repo(transcribe)仍是前端的唯一原始碼**:新 repo 裡只有一個
  `.github/workflows/sync.yml`,每 3 小時(以及可在該 repo 的 Actions 頁手動 Run workflow)
  自動從本 repo 複製 `index.html`、`v2/`、`byok/`、`privacy/`、`ads.txt` 過去。
  **改前端一律改本 repo**,新網址會自動跟上。
- 舊網址 `…/transcribe/…` 照常可用(頁面已加 `<link rel="canonical">` 指向新網址,
  告訴 Google 正式版在根網域)。
- (選用)Render 的 `SITE_V2_URL` 可改成 `https://hosanna0813-sys.github.io/v2/`,
  讓綠界付款導回直接到新網址、少一次跳轉;不改也能用(同網域,登入狀態共通)。
- **必做**:Supabase **Redirect URLs** 要加入 `https://hosanna0813-sys.github.io/v2/`
  (見上方「啟用 Email 登入」的警告框),否則從新網址登入會導錯。

### A. 免費試用的用量與防濫用(Render 環境變數)
試用花的是**你的** OpenAI 金鑰,所以用兩層每日上限保護荷包。到 Render → **Environment** 新增:
| 變數名 | 值 |
| --- | --- |
| `TRIAL_MINUTES` | 每個 IP 每天可免費試用的分鐘數(預設 `10`) |
| `TRIAL_DAILY_TOTAL_MINUTES` | **全站**每天免費試用總量上限(預設 `300`;設 `0` = 不限)。到量後訪客會被導去登入儲值 |

- 未設 `OPENAI_API_KEY` 時,免費試用會自動停用(首頁會提示登入使用),其餘功能不受影響。
- 單檔/單段上限就是 `TRIAL_MINUTES` 分鐘;超過會請訪客截取較短片段或登入使用。
- 另有既有的每小時請求數上限一起把關。
- 試用選項(對齊進階版):可勾「加時間軸 [時:分:秒]」「刪口頭贅詞」「推測講者(實驗)」,
  結果有「校正版/原始版」分頁、可直接編輯、可下載 Word(.doc)與 .txt;
  YouTube 預覽播放器可按「設為起點/終點」標記截取;選檔即預檢長度、
  伺服器喚醒逾時會自動重試。

### B. 放 AdSense 廣告
發布商 ID **`ca-pub-5508689053185527` 已填好**。AdSense 官方網站載入碼已放進**每一頁**的
`<head>`(`index.html`、`byok/index.html`、`v2/index.html`、`privacy/index.html`),供 Google
驗證網站;`ads.txt` 也已填入發布商號碼。首頁兩個版位(內容上方、結果下方)目前是
「待啟用」佔位框,不影響任何功能。版位代碼由 `index.html` `<head>` 的設定控制:
```html
<script>
window.ADS = {
  pub: 'ca-pub-5508689053185527',
  slotTop: '',    // ← 核准後填(內容上方版位代碼)
  slotBottom: ''  // ← 核准後填(結果下方版位代碼)
};
</script>
```

**目前狀態 / 步驟**
1. **已送審**:網站載入碼已在每頁 `<head>`,Google 可驗證網站、進行審核(通常數天)。
   審核期間 `slotTop`/`slotBottom` 留空即可,兩個版位維持佔位框,不影響網站。
2. **核准後開廣告**:在 AdSense 後台為「內容上方」「結果下方」各建立一個
   **多媒體廣告單元**,各會拿到一組版位代碼(一串數字)。把它們分別填進 `index.html` 的
   `slotTop`、`slotBottom`,存檔推上線 → 兩個版位開始顯示廣告。
   - 不用改 HTML 其他地方,程式會自動用發布商 ID + 版位代碼產生廣告。

**隱私權政策(已內建)**:AdSense 要求網站有隱私權政策。已新增 `privacy/index.html`
(網址 `…/transcribe/privacy/`),三個頁面頁尾都有連結。裡面已寫明 Google 廣告 Cookie 的
使用與停用方式;**請把該頁最後的聯絡信箱換成你要公開的信箱**(HTML 內已用註解標好位置)。
> 若你的訪客可能來自歐盟 / 英國,Google 另會要求顯示 Cookie 同意視窗(CMP)。可日後在
> AdSense 後台的「隱私權與訊息」啟用 Google 內建同意訊息,本次未加。

**ads.txt(已附範本,含 github.io 限制說明)**:根目錄有 `ads.txt` 範本。核准後把裡面的
`pub-XXXXXXXXXXXXXXXX` 換成你的發布商號碼(即 `ca-pub-…` 去掉開頭 `ca-`)。
> ⚠️ 限制:AdSense 只讀「**網域根目錄**」的 ads.txt。本站是 GitHub Pages **專案頁**
> (`…github.io/transcribe/`),這個檔會落在 `…/transcribe/ads.txt`,不是網域根,Google
> 讀不到,只會在後台顯示「earnings at risk」提醒——**不影響審核與廣告顯示、也不影響收入**。
> 想讓提醒消失,兩種正解:(1) 之後改綁**自訂網域**;(2) 另外建一個名為
> `hosanna0813-sys.github.io` 的 repo(使用者頁),把 `ads.txt` 放它根目錄,即成為
> `hosanna0813-sys.github.io/ads.txt`。可日後再處理。

---

# 正式上線環境(APP_ENV)與安全設定總表

Render → **Environment** 建議在正式收費前設定:
| 變數 | 說明 |
| --- | --- |
| `APP_ENV` | 設 `production`:金流缺正式金鑰時**直接停用儲值**(絕不用測試金鑰)、`PUBLIC_BASE_URL` 必填。未設=development |
| `RELAY_SHARED_SECRET` | 與家用機相同的中繼簽章密鑰(見上方 B-0) |
| `DIAG_TOKEN` | 不設 → `/diag` 一律 404(建議平時不設;要排查時暫時設一串亂碼,用 `X-Diag-Token` 標頭存取) |

健康檢查:`/healthz` 只代表 Web 行程存活;**`/readyz`** 檢查 ffmpeg、暫存目錄、
PO Token 產生器、金鑰設定,必要依賴故障回 503。部署後可開 `/readyz` 確認全綠。

免費試用防濫用現況:可信 IP(不受偽造 X-Forwarded-For 影響)+ 匿名裝置 ID(隨機
UUID)雙鍵計量 + 全站每日總量;失敗自動退還。⚠️ 計量目前存於伺服器記憶體,
**重啟會歸零**(Render 免費方案單實例,風險可控);持久化到資料庫列為下一階段工作。
