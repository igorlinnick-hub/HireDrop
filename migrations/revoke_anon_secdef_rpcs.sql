-- 2026-09-30: SECURITY DEFINER RPCs were callable with the public anon key.
-- claim_ai_use/release_ai_use take any p_user_id (burn or reset anyone's AI quota),
-- increment_invite_code_uses exhausts invite codes, affiliate_click_totals leaks
-- every affiliate code + clicks. Only the backend (service_role) calls them.
-- affiliate_stats stays: the website calls it and it scopes by auth.uid().
REVOKE EXECUTE ON FUNCTION public.claim_ai_use(uuid, integer)            FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.release_ai_use(uuid)                   FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.increment_invite_code_uses(text)       FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.affiliate_click_totals(timestamptz, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.claim_ai_use(uuid, integer)            TO service_role;
GRANT  EXECUTE ON FUNCTION public.release_ai_use(uuid)                   TO service_role;
GRANT  EXECUTE ON FUNCTION public.increment_invite_code_uses(text)       TO service_role;
GRANT  EXECUTE ON FUNCTION public.affiliate_click_totals(timestamptz, timestamptz) TO service_role;
