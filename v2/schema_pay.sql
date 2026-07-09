-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 金流:付款入帳(綠界 ECPay)
--
-- 前置:先跑過 schema.sql ~ schema_phase5.sql。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
--
-- 入帳只由後端 service_role 在「驗證綠界通知簽章成功」後呼叫。
-- 冪等:同一 payment 已 paid 就不重複加值;provider_payment_id 唯一索引為第二道防線。
-- =============================================================

-- 綠界訂單編號(MerchantTradeNo,≤20 英數);回呼時用它找回這筆付款
alter table public.payments add column if not exists merchant_trade_no text;
create unique index if not exists payments_merchant_trade_no
  on public.payments (merchant_trade_no) where merchant_trade_no is not null;

create or replace function public.credit_payment(
  p_payment_id  uuid,
  p_provider_txn text,
  p_raw         jsonb
)
returns table (ok boolean, remaining integer, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_user   uuid;
  v_status text;
  v_credits integer;
  v_balance integer;
begin
  -- 鎖定並讀取該筆付款
  select user_id, status, credits_added
    into v_user, v_status, v_credits
    from public.payments where id = p_payment_id for update;

  if not found then
    return query select false, null::integer, 'payment_not_found'; return;
  end if;

  -- 已入帳 → 冪等回覆(不重複加值)
  if v_status = 'paid' then
    return query select true,
      (select remaining_credits from public.credit_balances where user_id = v_user),
      'duplicate';
    return;
  end if;

  if v_credits is null or v_credits <= 0 then
    return query select false, null::integer, 'invalid_credits'; return;
  end if;

  -- 標記已付款(provider_payment_id 撞唯一索引 → 視為重複)
  begin
    update public.payments
       set status = 'paid',
           provider_payment_id = p_provider_txn,
           raw_payload = p_raw,
           paid_at = now()
     where id = p_payment_id;
  exception when unique_violation then
    return query select true,
      (select remaining_credits from public.credit_balances where user_id = v_user),
      'duplicate';
    return;
  end;

  -- 加值 + 寫流水帳
  insert into public.credit_balances (user_id, remaining_credits)
    values (v_user, 0) on conflict (user_id) do nothing;
  update public.credit_balances
     set remaining_credits = remaining_credits + v_credits, updated_at = now()
   where user_id = v_user
  returning remaining_credits into v_balance;

  insert into public.credit_transactions (user_id, type, amount_credits, reason, related_payment_id)
  values (v_user, 'add', v_credits, 'ecpay_topup', p_payment_id);

  return query select true, v_balance, null::text;
end;
$$;

revoke all on function public.credit_payment(uuid,text,jsonb) from public, anon, authenticated;
grant  execute on function public.credit_payment(uuid,text,jsonb) to service_role;
