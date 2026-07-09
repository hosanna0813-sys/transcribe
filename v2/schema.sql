-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段一資料庫結構(Supabase / Postgres)
--
-- 用法:在 Supabase 專案的 SQL Editor 貼上整份執行一次即可。
-- 本腳本可重複執行(idempotent):重跑不會出錯,方便日後調整後再跑一次。
--
-- 安全地基(規畫書第 8 節最高優先):
--   * 五張金錢/用量資料表全部啟用 RLS。
--   * 前端(anon / authenticated)只能「讀自己的資料」,一律禁止寫入。
--   * 使用者無法把自己設成 admin(profiles.role 前端不可改)。
--   * 所有餘額變動日後只由後端 service_role 執行(service_role 天生繞過 RLS)。
-- =============================================================

-- ---------- 1. profiles(對應 auth.users) ----------
create table if not exists public.profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  email        text,
  display_name text,
  role         text not null default 'user',   -- user / admin
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ---------- 2. credit_balances(剩餘秒數,1 credit = 1 秒) ----------
create table if not exists public.credit_balances (
  user_id           uuid primary key references auth.users(id) on delete cascade,
  remaining_credits integer not null default 0,
  updated_at        timestamptz not null default now()
);

-- ---------- 3. credit_transactions(流水帳,永久保留) ----------
create table if not exists public.credit_transactions (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references auth.users(id) on delete cascade,
  type               text not null,            -- add / deduct / refund / adjust
  amount_credits     integer not null,         -- 可正可負
  reason             text,
  related_usage_id   uuid,
  related_payment_id uuid,
  idempotency_key    text unique,              -- 防重複加值 / 退款
  created_at         timestamptz not null default now()
);

-- ---------- 4. usage_logs(每次任務一筆,帳務長期保留) ----------
create table if not exists public.usage_logs (
  id                        uuid primary key default gen_random_uuid(),
  user_id                   uuid not null references auth.users(id) on delete cascade,
  source_type               text,             -- upload / youtube
  source_name               text,             -- 檔名或 YouTube 標題(使用者刪除內容後設為 null)
  youtube_url               text,
  start_seconds             integer,
  end_seconds               integer,
  duration_seconds          integer,
  credits_reserved          integer,
  credits_charged           integer,
  transcription_model       text,
  correction_model          text,
  prompt_tokens             integer,
  completion_tokens         integer,
  estimated_openai_cost_usd numeric,
  applied_pricing           jsonb,            -- 當次適用費率快照(日後查帳還原歷史價格)
  status                    text not null default 'reserved',  -- reserved/processing/completed/failed/refunded
  error_message             text,
  created_at                timestamptz not null default now(),
  completed_at              timestamptz
);

-- 同帳號同時最多一個進行中的任務(DB 層保證,規畫書第 9 節)
create unique index if not exists one_active_job
  on public.usage_logs (user_id)
  where status in ('reserved', 'processing');

-- ---------- 5. payments(付款,永久保留) ----------
create table if not exists public.payments (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references auth.users(id) on delete cascade,
  provider            text not null default 'manual',  -- manual/ecpay/newebpay/tappay/stripe
  provider_payment_id text,
  amount              numeric,
  currency            text not null default 'TWD',
  credits_added       integer,
  status              text not null default 'pending',  -- pending/paid/failed/refunded
  raw_payload         jsonb,
  created_at          timestamptz not null default now(),
  paid_at             timestamptz
);

-- provider + provider_payment_id 唯一(防金流 webhook 重送重複加值)
create unique index if not exists payments_provider_unique
  on public.payments (provider, provider_payment_id)
  where provider_payment_id is not null;

-- =============================================================
-- 新使用者觸發器:註冊後自動建立 profile 與 credit_balance(0 元起)
-- =============================================================
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (id, email)
    values (new.id, new.email)
    on conflict (id) do nothing;
  insert into public.credit_balances (user_id, remaining_credits)
    values (new.id, 0)             -- 每日免費額度是階段五的事,階段一先給 0
    on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- =============================================================
-- RLS:全部啟用,前端只能讀自己的資料、一律禁止寫入
-- =============================================================
alter table public.profiles            enable row level security;
alter table public.credit_balances     enable row level security;
alter table public.credit_transactions enable row level security;
alter table public.usage_logs          enable row level security;
alter table public.payments            enable row level security;

-- profiles:只能讀自己(用 id 比對)
drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
  for select using (auth.uid() = id);

-- 其餘四張:只能讀自己(用 user_id 比對)
drop policy if exists credit_balances_select_own on public.credit_balances;
create policy credit_balances_select_own on public.credit_balances
  for select using (auth.uid() = user_id);

drop policy if exists credit_transactions_select_own on public.credit_transactions;
create policy credit_transactions_select_own on public.credit_transactions
  for select using (auth.uid() = user_id);

drop policy if exists usage_logs_select_own on public.usage_logs;
create policy usage_logs_select_own on public.usage_logs
  for select using (auth.uid() = user_id);

drop policy if exists payments_select_own on public.payments;
create policy payments_select_own on public.payments
  for select using (auth.uid() = user_id);

-- 注意:以上「只給 select」政策,等於前端 anon/authenticated 完全無法
-- insert/update/delete 這五張表(包含 profiles.role,使用者無法自我升級為 admin)。
-- 後端 service_role 金鑰天生繞過 RLS,日後所有餘額變動由後端執行。

-- =============================================================
-- 【階段一測試用】手動給自己一些 Credits(還沒有 admin 加值介面)
-- 先在網站用 Email 登入一次讓帳號進 auth.users,再回來把下面的 email
-- 換成你的登入信箱、取消註解執行。1200 credits = 20 分鐘。
-- =============================================================
-- update public.credit_balances
--   set remaining_credits = 1200, updated_at = now()
--   where user_id = (select id from auth.users where email = 'YOUR_EMAIL@example.com');
