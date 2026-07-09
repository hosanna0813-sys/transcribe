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

## 接下來的階段(尚未實作)

| 階段 | 內容 |
| --- | --- |
| 二 | OpenAI 後端化;短音檔跑通「預扣 → 轉錄 → 結算/退款」 |
| 三 | 長音檔背景排程 + 切段;音檔任務後自動刪除、24 小時清理 |
| 四 | 使用紀錄列表;admin 手動加值介面;流水帳;使用者刪除紀錄 |
| 五 | 每日免費額度、長度上限、rate limit、冪等鍵、watchdog 退款 |
| 六 | payments 多金流介面;每日監控報表;價格後端化 |
