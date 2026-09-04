-- Atomic daily AI-quota claim (fix/ai-cap-race).
--
-- Why: the old flow was check -> LLM call -> increment. The window between the
-- check and the increment is the WHOLE generation (seconds), so N parallel
-- requests could each pass the check and burn Anthropic spend past the daily
-- cap; increment_today itself was also read-then-write (lost updates).
-- claim_ai_use consumes the slot in ONE statement, before any money is spent.
--
-- Backend falls back to the old two-step flow until this is applied, so the
-- migration and the code merge can land in either order.

create or replace function public.claim_ai_use(p_user_id uuid, p_limit int)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  claimed int;
begin
  if p_limit <= 0 then
    return false;
  end if;
  insert into public.cover_letter_usage as u (user_id, date, count, updated_at)
  values (p_user_id, current_date, 1, now())
  on conflict (user_id, date) do update
    set count = u.count + 1, updated_at = now()
    where u.count < p_limit
  returning count into claimed;
  -- WHERE blocked the update -> no row returned -> quota exhausted.
  return claimed is not null;
end $$;

-- Best-effort refund after a generation that FAILED (nothing was spent), so an
-- Anthropic outage can't eat a user's whole daily quota one error at a time.
create or replace function public.release_ai_use(p_user_id uuid)
returns void
language sql
security definer
set search_path = public
as $$
  update public.cover_letter_usage
     set count = greatest(count - 1, 0), updated_at = now()
   where user_id = p_user_id and date = current_date;
$$;
