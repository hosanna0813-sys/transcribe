-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 使用紀錄:刪除紀錄內容(去內容化)
--
-- 前置:先跑過 schema.sql ~ schema_phase5.sql。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
--
-- 使用紀錄「列表」由前端直接以 RLS 讀取 usage_logs(只讀自己那筆),不需函式。
-- 「刪除內容」需要清除欄位,但 RLS 禁止前端寫入 usage_logs,故用一個
-- security definer 函式:內部以 auth.uid() 限定只能清除「自己」那筆的內容欄位,
-- 帳務欄位(秒數、credits、狀態、時間)一律保留。
-- =============================================================

create or replace function public.delete_usage_content(p_usage_id uuid)
returns boolean
language plpgsql
security definer set search_path = public
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is null then
    return false;   -- 未登入
  end if;
  update public.usage_logs
     set source_name = null,
         youtube_url = null,
         result_text = null
   where id = p_usage_id
     and user_id = v_uid;      -- 只能清除自己那筆
  return found;
end;
$$;

-- 讓已登入使用者可直接呼叫(函式內已用 auth.uid() 限定本人)
revoke all on function public.delete_usage_content(uuid) from public, anon;
grant execute on function public.delete_usage_content(uuid) to authenticated, service_role;
