-- =============================================================
-- 階段十:歷史紀錄保存逐字稿(登入者可回看/重新下載)
-- 冪等,可重複執行。前置:已跑過 schema_phase9.sql。
--
-- 變更前的設計是「隱私優先」:結果被前端取走一次即清空(claim_result)、24 小時過期。
-- 本階段改為:
--  * 新 RPC read_result:讀取結果「不清空」;後端輪詢與歷史檢視都用它。
--    claim_result 保留不動(部署順序 fallback:後端在 phase10 未套用時退回舊行為)。
--  * complete_transcription 的結果保留時間預設 24 → 720 小時(30 天);
--    到期仍由既有 expire_results 去內容化,使用者也可隨時「刪除此紀錄內容」。
--  * 前端歷史列表以 RLS 直讀自己的 result_text(select-own 政策本就涵蓋,無需新權限)。
-- =============================================================

-- ---------- 讀取結果(不清空;僅 service_role) ----------
create or replace function public.read_result(p_usage_id uuid, p_user_id uuid)
returns table (result_text text, status text, credits_charged integer)
language plpgsql
security definer set search_path = public
as $$
begin
  return query
  select u.result_text, u.status, u.credits_charged
    from public.usage_logs u
   where u.id = p_usage_id and u.user_id = p_user_id;
end;
$$;

revoke all on function public.read_result(uuid,uuid) from public, anon, authenticated;
grant execute on function public.read_result(uuid,uuid) to service_role;

-- ---------- 結果保留 30 天(僅改 TTL 預設;其餘與階段七完全相同) ----------
drop function if exists public.complete_transcription(uuid, uuid, integer, numeric, text, integer, integer, jsonb);

create or replace function public.complete_transcription(
  p_usage_id          uuid,
  p_user_id           uuid,
  p_actual_seconds    integer,
  p_cost_usd          numeric,
  p_result_text       text    default null,
  p_prompt_tokens     integer default 0,
  p_completion_tokens integer default 0,
  p_pricing           jsonb   default null
)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_reserved     integer;
  v_free_res     integer;
  v_paid_res     integer;
  v_charge       integer;
  v_charge_free  integer;
  v_charge_paid  integer;
  v_refund_free  integer;
  v_refund_paid  integer;
  v_ttl_hours    integer := coalesce(nullif(current_setting('app.result_ttl_hours', true),'')::int, 720);
  v_balance      integer;
begin
  select credits_reserved, coalesce(free_credits, 0)
    into v_reserved, v_free_res
    from public.usage_logs
   where id = p_usage_id and user_id = p_user_id and status = 'processing'
   for update;

  if not found then
    return query select false,
      (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;

  v_paid_res := v_reserved - v_free_res;
  v_charge := least(coalesce(p_actual_seconds, v_reserved), v_reserved);
  v_charge_free := least(v_charge, v_free_res);
  v_charge_paid := v_charge - v_charge_free;
  v_refund_free := v_free_res - v_charge_free;
  v_refund_paid := v_paid_res - v_charge_paid;

  update public.usage_logs
     set status = 'completed',
         credits_charged = v_charge,
         charged_free = v_charge_free,
         charged_paid = v_charge_paid,
         estimated_openai_cost_usd = p_cost_usd,
         prompt_tokens = p_prompt_tokens,
         completion_tokens = p_completion_tokens,
         applied_pricing = p_pricing,
         result_text = p_result_text,
         completed_at = now(),
         expires_at = now() + make_interval(hours => v_ttl_hours)
   where id = p_usage_id;

  if v_refund_paid > 0 then
    update public.credit_balances
       set remaining_credits = remaining_credits + v_refund_paid, updated_at = now()
     where user_id = p_user_id;
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
    values (p_user_id, 'refund', v_refund_paid, 'transcribe_settle_diff', p_usage_id);
  end if;
  if v_refund_free > 0 then
    update public.credit_balances
       set free_used_today = greatest(0, free_used_today - v_refund_free), updated_at = now()
     where user_id = p_user_id;
  end if;

  select remaining_credits into v_balance from public.credit_balances where user_id = p_user_id;
  return query select true, v_balance;
end;
$$;

revoke all on function public.complete_transcription(uuid,uuid,integer,numeric,text,integer,integer,jsonb)
  from public, anon, authenticated;
grant execute on function public.complete_transcription(uuid,uuid,integer,numeric,text,integer,integer,jsonb)
  to service_role;
