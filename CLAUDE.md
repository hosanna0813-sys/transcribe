# CLAUDE.md — 本專案開發指南(每次對話自動載入)

語音轉逐字稿服務(繁中市場)。三頁靜態前端(GitHub Pages)+ FastAPI 後端(Render 免費方案)
+ Supabase(登入/帳務)+ 家用中繼(住宅 IP 抓 YouTube)+ 綠界 ECPay 金流。
營運者非技術背景,以繁體中文溝通;所有註解、訊息、文件一律繁中。

## 檔案地圖

| 路徑 | 用途 |
|---|---|
| `index.html` | 首頁:免登入免費試用(每 IP/裝置每日 10 分鐘,走 `/api/trial`,營運者金鑰付費) |
| `v2/index.html` | 計費版:Supabase 登入、預付分鐘數、背景任務(`/api/jobs` 輪詢)、儲值、紀錄 |
| `byok/index.html` | 進階版:使用者自備 OpenAI Key(此頁**不放 AdSense**,有金鑰輸入框) |
| `privacy/index.html` | 隱私權政策(AdSense 必備) |
| `server/main.py` | 後端主程式;**同一份程式也跑在家用中繼**(角色由環境變數決定) |
| `server/pricing.py` `ecpay.py` `relay.py` | 價格/成本統計、綠界、HMAC 中繼簽章 |
| `server/home/` | 家用中繼 Docker 部署(docker-compose + ngrok;README 有完整教學含第二台備援) |
| `v2/schema.sql` ~ `schema_phase9.sql` | Supabase migration,**依序執行、各檔冪等** |
| `tests/` | pytest 全套;`scripts/check_frontend.py` 前端靜態檢查 |
| `上線檢查清單.md` | 營運者待辦(收費前必做、安全建議);`協作指南.md` 給營運者的協作 SOP |

## 架構關鍵事實(改code前先記住)

- **三頁前端各自內嵌重複的 CSS token**(`:root`/`[data-mode=dark]` 三處一模一樣):
  改設計要三處同步;三頁功能已對齊(來源分頁/拖放/預檢/YT 預覽標記/四選項/
  結果雙版本/Word+txt 下載),改共用功能時三頁一起看。
- **後端與家用中繼是同一份 main.py**:中繼端行為改動(`/audio`、`/info`、
  `_fetch_and_transcode`、槽位/逾時)要營運者在家用機 `docker compose up -d --build`
  才生效——PR 說明務必註明。Render 端(worker、API)合併即自動部署。
- **`HOME_RELAY_URL` 支援逗號分隔多台**(備援/分流,`_relay_urls()` 依序嘗試);
  所有中繼共用同一 `RELAY_SHARED_SECRET`。**絕不在 Render 端 probe YouTube**
  (機房 IP 會被 bot 驗證擋),一律走 `_relay_info()`/`_relay_fetch_audio()`。
- **任務佇列 = DB**(`usage_logs` status: queued→processing→completed/failed/cancelled)
  + 就地 worker 執行緒(免費方案無獨立 Worker 服務)。記憶體 `_v3_jobs` 只放進度顯示,
  **不可**當帳務/狀態的唯一來源。任務選項在 `usage_logs.options`(jsonb),重試不掉。
- **帳務**:1 credit = 1 秒;預扣→完成結算→按來源退款(免費退免費計數、付費退餘額);
  所有餘額變動走 security-definer RPC,前端 RLS 只能讀自己。
- **進度帶**:取音訊 1–38(估算)→ 轉檔 38–40 → 轉錄 40–95(真實)→ 校正 95–99(真實)。
- 併發參數:`WORKER_CONCURRENCY`(預設 1)、`CHUNK_WORKERS`(轉錄並行,預設 6)、
  校正並行 3;中繼 `_audio_slots=2`、`RELAY_SLOT_WAIT_SECONDS=180`、`FETCH_MAX_SECONDS=1500`。

## 安全鐵則(營運者稽核規格,不可違反)

1. 機密/網址/金鑰只走環境變數,**不可硬編碼在公開前端**(Supabase anon key 是唯一例外,屬公開值)。
2. 不在 console、錯誤訊息、日誌輸出完整 API Key 或任何機密。
3. CORS 不當驗證機制;隱藏網址不當安全機制(中繼靠 HMAC 簽章,不靠 ngrok 網址保密)。
4. 記憶體 dict 不當正式帳務/額度資料庫(帳務一律 Supabase RPC)。
5. `APP_ENV=production` 時 fail closed:缺正式金鑰直接停用該功能,**絕不退回測試金鑰**。
6. 中繼請求必帶 HMAC(timestamp±60s、nonce 防重放、compare_digest)。
7. 模型識別字(claude-*)不得出現在 commit、PR、程式碼、任何推上 repo 的產物。

## 開發慣例

- **migration**:新檔 `v2/schema_phaseN.sql`,冪等可重跑;**函式簽名/回傳型別變更一律
  drop-first**(舊簽名也 drop,避免重載歧義);結尾補 revoke/grant(僅 service_role)。
- **部署順序防護**(必做):後端呼叫新 RPC 要能容忍 migration 還沒跑——捕捉 PGRST202,
  fallback 到舊簽名或回明確的 503 中文訊息(前例:`api_create_job` 的 p_options fallback、
  `_trial_reserve` 記憶體 fallback)。
- **測試**:每批改動配 pytest(mock 為主,不需網路);SQL/worker 測試需本機 Postgres:
  ```
  pip install psycopg
  mkdir -p /tmp/pg && chown postgres:postgres /tmp/pg
  su postgres -c "/usr/lib/postgresql/16/bin/initdb -D /tmp/pg/data -E UTF8 --locale=C"
  su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/pg/data -l /tmp/pg/pg.log -o '-k /tmp/pg -p 5433 -c listen_addresses=' start"
  su postgres -c "psql -h /tmp/pg -p 5433 -c 'create database testdb' postgres"
  DATABASE_URL="postgresql://postgres@/testdb?host=/tmp/pg&port=5433" python -m pytest -q --ignore=tests/test_jwt.py
  ```
  (`test_jwt.py` 在部分容器因 cffi 壞掉無法收集,CI 會跑;其餘應全綠。)
  另跑 `python -m py_compile server/main.py`、`python scripts/check_frontend.py`;
  改前端 inline JS 後用 node `new Function()` 做語法檢查。
- 新環境變數要同步三處:`.env.example`、`render.yaml`、(若營運者需設定)`上線檢查清單.md`。

## 流程 SOP(血淚教訓,務必遵守)

1. 開工先 `git fetch origin main && git checkout -B claude/optimization-wdw4yu origin/main`
   (前一個 PR 已合併時必做;有未合併 commit 則 rebase 保留)。
2. **一批改動 = 一個 PR;PR 開出後不要再往同分支疊推新批次**。
   (事故:PR #47 開出 1 分鐘就被營運者合併,之後疊推的兩批一直沒上線,
   直到發現才 rebase 開 #48 補救。營運者看到 PR 就會馬上按合併——這是常態,要配合。)
3. PR 說明用白話繁中,「部署後(營運者)」段落逐一點名三類待辦:
   Supabase SQL(哪個檔)/ Render 環境變數(哪個 key)/ 家用中繼是否需要重建。
4. 合併後主動驗證:`curl /readyz`、`/openapi.json` 找新欄位、根網域 grep 新前端標記;
   提醒營運者 Ctrl+Shift+R 強制刷新。
5. 前端自動同步到根網域 `hosanna0813-sys.github.io` 每 3 小時一次(該 repo 的 sync
   workflow;等不及請營運者到該 repo Actions 手動 Run workflow)。
   注意:該 repo **不在**本 session 的 GitHub 權限範圍內,不要嘗試直接改它。

## 現況快照(2026-07 底)

- 已完成:安全總檢三階段(HMAC/JWT 白名單/可信 IP/綠界回呼驗證)、帳務退款按來源、
  試用持久化、成本統計、DB 佇列+就地 worker、多台中繼備援、500MB 上限、
  長音檔提速(並行/去重複轉檔)、三頁介面與功能對齊、schema 到 **phase9**(營運者已全跑)。
- 未完(見 `上線檢查清單.md`):收費前 B 區(`APP_ENV=production`、綠界正式金鑰、實測一筆
  真實付款)、AdSense 核准後填版位代碼、第二台家用中繼(教學在 server/home/README.md)。
