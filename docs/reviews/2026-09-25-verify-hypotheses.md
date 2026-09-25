# Верификация гипотез bug-swarm — 2026-09-25

Прогон `wf_849e0148-99d` (21 агент): 7 гипотез из отчёта 09-19, каждая — проверяющий + 2 скептика.
Правило: скептик обязан ОПРОВЕРГНУТЬ; подтверждение только при 2/2.

Итог: **6 CONFIRMED**, 0 plausible, 1 опровергнуто.

## CONFIRMED

### [HIGH] `app/routers/billing.py:193`

Stripe webhook writes the idempotency marker BEFORE handling and always returns 200, so any mid-handler failure permanently consumes the event and loses the tier grant

**Сценарий:** 1) User U pays; Stripe sends checkout.session.completed evt_1 {client_reference_id: U, customer: cus_X, subscription: sub_Y}. 2) billing.py:193 calls billing_db.mark_event_processed(evt_1) which (app/db/billing.py:65-76) UPSERTs the row into stripe_events and returns True -> the event is recorded as processed before a single line of handling runs. 3) billing.py:203 link_customer succeeds. 4) billing.py:205 stripe.Subscription.retrieve(sub_Y) raises on a transient Stripe 5xx/timeout (a live network call made inside the handler). 5) The except at billing.py:244-247 swallows it with only a stderr print, and line 249 returns {"received": True} with HTTP 200. billing_db.grant() at billing.py:137 never ran, so profiles.subscription_tier stays 'free' for a paying customer. 6) Stripe sees 200 and never retries. 7) Even a manual resend from the Stripe dashboard is useless: line 193 re-reads the already-inserted stripe_events row, mark_event_processed returns False, and line 194 returns {"received": True, "duplicate": True} without executing the branch. The dedup row is never deleted or rolled back on failure, and grep shows no reconciliation/backfill job touches stripe_events (only app/routers/billing.py and app/db/billing.py). The same trace applies when the Supabase UPDATE inside billing_db.grant (app/db/billing.py:43) raises. Result: paid user permanently on the free tier, recoverable only by hand-editing the DB.

```
billing.py:193 `if event_id and not billing_db.mark_event_processed(event_id, etype): return {"received": True, "duplicate": True}` sits ABOVE the `try:` at line 196. app/db/billing.py:57-76 mark_event_processed performs the INSERT itself and returns bool(res.data) — True only on fresh insert. billing.py:244-249 `except Exception as e: print(...)` then `return {"received": True}` — no non-2xx is ever returned from the handler body, and the comment 'A missed grant self-heals on the next event' is wrong because the next event carries a different event id for a different invoice and never re-attempts the consumed one. tests/test_billing.py:144-149 confirms the skip path asserts grant is not called.
```

### [HIGH] `app/routers/billing.py:215`

invoice.paid delivered before checkout.session.completed is marked processed then early-returned, permanently losing the affiliate commission for the first invoice

**Сценарий:** 1) Referred user U (row in referrals, active affiliate) completes checkout. Stripe emits both invoice.paid (evt_A) and checkout.session.completed (evt_B); delivery order is not guaranteed — migrations/2026-07-stripe-events.sql:2 states Stripe 'can send them out of order'. 2) evt_A arrives first. billing.py:193 marks evt_A processed in stripe_events (returns True). 3) billing.py:213 calls billing_db.find_user_by_customer(cus_X), which (app/db/billing.py:19-28) selects profiles WHERE stripe_customer_id = cus_X. That column is still NULL: link_customer is only called in the checkout.session.completed branch at billing.py:203, and create_checkout (billing.py:62-70) never creates/stores a customer id. So user_id is None. 4) billing.py:214-215 returns {"received": True} with HTTP 200 — no grant, and critically billing.py:230-231 affiliates_db.accrue_from_invoice never executes. 5) evt_B arrives later, links the customer and grants the tier, so the user's tier looks correct and nothing appears wrong. 6) The commission for that invoice is gone forever: app/db/affiliates.py:53 accrue_from_invoice is the ONLY accrual path, keyed on stripe_invoice_id, and it is only ever reached from billing.py:231 on invoice.paid. A Stripe resend of evt_A hits billing.py:193 -> mark_event_processed returns False -> line 194 returns duplicate. No reconciliation job exists (stripe_events is referenced only by app/routers/billing.py and app/db/billing.py). 7) Because that first invoice is also what sets referrals.first_paid_at and status='paying' (app/db/affiliates.py:100-103), the referral never flips to paying either, so the affiliate is unpaid and the referral looks non-converting.

```
billing.py:213-215 `user_id = billing_db.find_user_by_customer(customer_id) if customer_id else None` / `if not user_id: return {"received": True}` — returns 200 after the dedup row was already written at line 193, with no compensating delete. link_customer is reachable only from billing.py:203 inside the checkout.session.completed branch. app/db/affiliates.py:3-6 documents invoice.paid as the sole accrual entry point, and its docstring at lines 10-12 places idempotency in commissions.stripe_invoice_id UNIQUE — which only helps if accrue_from_invoice is actually called at least once.
```

### [HIGH] `app/routers/billing.py:62`

Checkout has no already-subscribed guard and never reuses the Stripe customer — second checkout double-bills and orphans the first subscription beyond the portal

**Сценарий:** 1) User on free tier opens /dashboard/settings?tab=billing. BillingSection.tsx L110 computes isPaid from /stats; it is false, so the "Choose Weekly" button renders (L189-199).
2) Click -> POST /billing/checkout. create_checkout (billing.py L50-74) reads nothing from profiles and checks no tier; it calls stripe.checkout.Session.create(mode="subscription", customer_email=..., ...) at L62-70 WITHOUT customer=. Stripe mints a brand-new Customer cus_A and subscription sub_A on payment (Stripe never dedupes customers by email).
3) Webhook checkout.session.completed -> L203 billing_db.link_customer(user, cus_A) writes profiles.stripe_customer_id = cus_A; L208 grants the tier.
4) The dashboard tab still holds the pre-payment tier: checkout was opened in a NEW tab (BillingSection.tsx L80/L85), and the onFocus re-read (L67) can fire before the webhook lands and still get tier="free". Independently, the /stats load is swallowed by a bare `catch {}` (L62-64), leaving tier=null -> isPaid=false -> buttons shown to a paying user forever. Either way the user clicks "Choose Monthly" (the card literally invites it).
5) Second POST /billing/checkout: still no guard, still no customer= -> Stripe creates cus_B + sub_B. The user is now billed on BOTH sub_A ($12/wk) and sub_B ($39/mo) simultaneously.
6) checkout.session.completed for cus_B -> link_customer (app/db/billing.py L12-16) does an unconditional UPDATE, overwriting stripe_customer_id with cus_B despite its docstring "set once at first checkout".
7) Consequence A: create_portal (L86-95) resolves only the stored cus_B, so the portal shows sub_B only — sub_A has no cancel path anywhere in the product.
8) Consequence B: every later invoice.paid for sub_A carries customer=cus_A; find_user_by_customer(cus_A) (L213 -> app/db/billing.py L19-28) now matches no profile row, so L214-215 returns {"received": True} and the event is silently dropped — no grant refresh and no affiliates_db.accrue_from_invoice (L230-231) for that collected money — while Stripe keeps charging the card indefinitely.

```
billing.py L62-70 Session.create(mode="subscription", line_items=[...], client_reference_id=user.id, customer_email=getattr(user, "email", None), ...) — no `customer=` and no profile lookup; `grep -n "customer=" app/` over the backend returns only L99 (portal) and L158 (Subscription.list), never the checkout path. L203 billing_db.link_customer(user_id, customer_id) -> app/db/billing.py L14 `.table("profiles").update({"stripe_customer_id": customer_id}).eq("user_id", user_id)` (unconditional overwrite). L93 portal reads that single stored id. L213-215 `user_id = billing_db.find_user_by_customer(customer_id) ...; if not user_id: return {"received": True}`.
```

### [HIGH] `app/routers/jobs.py:187`

on_search_filter (shared read-time gate for deck + auto ATS queue) never re-applies the salary filter

**Сценарий:** on_search_filter is the single documented "one rule, one place" re-filter for the INSERT-only pool; it is called by get_ats_queue (jobs.py:235) and get_deck (jobs.py:296). Its return expression (jobs.py:187-202) applies keyword_match, is_generic_talent_pool, names_other_profession, matches_job_type, names_foreign_country and location_verdict — salary is absent. Steps: (1) user runs a sweep with salary_min unset -> _run_ats_discovery's filter_by_salary (jobs.py:473) is a no-op (salary_filter.py:104 returns early) and 160 Greenhouse/Ashby rows land in the pool, including $55k ones; (2) user then sets salary_min=150000 via POST /profile (profile.py:105-110, persisted by db/profile.py:190-198); (3) the pool is INSERT-only so nothing is re-evaluated; (4) next campaign calls GET /jobs/ats-queue?platform=greenhouse -> on_search_filter passes the $55k rows through, fresh_enough passes them, and the extension auto-applies. The salary preference is retroactively inert for every row already banked, on every platform. This is exactly the collection-time-filter-not-re-applied-at-read/submit case: the country gate got its read-time twin at jobs.py:201, salary never did.

```
jobs.py:187-202 is the full filter list and contains no salary predicate; grep for filter_by_salary/passes_salary across the repo returns only jobs.py:380 and jobs.py:473 (both collection-time) plus tests. The data needed is present: descriptions are persisted (app/db/jobs.py:128) and passes_salary reads description/title (modules/salary_filter.py:88-90).
```

### [HIGH] `app/routers/campaign.py:256`

campaign_start omits salary prefs from campaignFilters, so the native Indeed/ZipRecruiter auto-walk has no salary gate at any point

**Сценарий:** Indeed and ZipRecruiter auto-apply never route through the pool or through /jobs/ats-queue: background.js:898-901 (pickNextStage) sends them to the native walk, and content.js applies straight from the search page. The filters dict built at campaign.py:256-268 carries keywords, kw_cursor, platforms, location, job_type and search_radius_miles only; CampaignStartRequest (app/schemas.py:82-86) has no salary field either. Steps: (1) user sets salary_min=150000; (2) POST /campaign/start -> filters (no salary) -> campaign_db.start -> chrome.storage campaignFilters (background.js:1905); (3) content.js:4350-4378 builds the Indeed URL from those filters (q, l, radius, jt, iafilter, sort) with no salary parameter; (4) phase1 collects every Easy Apply card into pendingJobs (content.js:1408) and applies to them in order. Result: a $45k posting on page 1 is auto-submitted even though the user's floor is $150k. There is no client-side salary check anywhere in chrome-extension/content.js (the only salary references, lines 2866 and 3060-3062, are form-FILLING of desired_salary, not filtering).

```
campaign.py:256-268 filters dict; app/schemas.py:82-86 CampaignStartRequest; content.js:4350-4378 URL builder; content.js:1408 pendingJobs. grep 'salary' over chrome-extension/*.js yields only answer-filling code.
```

### [HIGH] `app/routers/jobs.py:737`

POST /jobs/ingest (the only Indeed/ZipRecruiter pool source) writes harvested cards without applying filter_by_salary

**Сценарий:** ingest_jobs is the sole path by which Indeed/ZR rows enter the pool (its own docstring, jobs.py:674-678, plus SERVER_SCRAPE_SKIP at jobs.py:110 which bars both from server-side find_jobs). The candidate filter at jobs.py:675-681 checks only platform/link/title; rows are built at jobs.py:686-701, optionally scored at jobs.py:720-735, and saved at jobs.py:737 with no salary call. Steps: (1) user sets salary_min=150000; (2) campaign walks an Indeed page; content.js:1390-1401 posts ~15 cards, each with the card snippet as description (which is where Indeed's pay text lives -- see app/db/jobs.py:413-418, 'a salary string for 86% of our applications'); (3) a '$22/hr' card is inserted as status='new', platform='indeed'; (4) the pool is INSERT-only so it is never re-checked; (5) get_deck (jobs.py:253) offers it as a Tap card, or -- once approved -- campaign_queue (campaign.py:74-79) hands it to the extension for submission. The salary data was in hand at ingest time and was discarded. Contrast the country gate, which IS applied in the sibling collection paths (jobs.py:373-375 and 469-471).

```
jobs.py:675-681 candidate filter, jobs.py:686-701 row build, jobs.py:737 save_jobs_bulk -- no filter_by_salary; compare jobs.py:378-380 and jobs.py:473 where it is applied. IngestJob schema (app/schemas.py:100-108) carries description, which is what passes_salary parses.
```

## REFUTED (НЕ чинить)

### `jobflow/scripts/night_shift/executor.py:607` — Night-shift executor saves applications without ever incrementing the 40-app free-taste counter

REFUTED against the code as it actually stands on disk at /Users/igorlinnik/Code/JobFlow/jobflow/scripts/night_shift/executor.py. The executor DOES advance the lifetime free-taste counter, in exactly the place the claim says it is missing:

- Line 46: `from app.db.subscriptions import check_can_apply, increment_free_apps  # noqa: E402`
- Line ~578: `gate = check_can_apply(user_id, "greenhouse", profile.get("email"))` immediately before the submit click, with an abort + activity_log line when `not gate["allowed"]`.
- Lines 628-646, right after the confirmed submit: `jobs_db.mark_applied_by_link(...)` -> `apps_db.save_application(...)` -> a comment stating precisely the claim's concern ("The lifetime free-taste counter lives beside the daily cap and is advanced by POST /applications/save, which this path bypasses...") -> `if gate.get("tier") == "free": with contextlib.suppress(Exception): increment_free_apps(user_id)`.

The guard is correct: check_can_apply's allowed-path return dict (app/db/subscriptions.py:236-245) always carries "tier": tier, which is "free" for a free user and "admin" for ADMIN_EMAILS (line 181), so the increment fires for exactly the tier the paywall counts. inc

## Не проверялось (не влезло в кап 7)

- **[high]** `jobflow/app/db/subscriptions.py:207` — Cap enforcement counts 'today' on the server's UTC date while the extension's pre-submit rail counts the browser's local day — users west of UTC lose their whole daily budget and leak an unrecorded application to the employer
- **[high]** `modules/platforms/craigslist.py:125` — as_completed(timeout=20) TimeoutError escapes CraigslistPlatform.scrape and 500s the whole /jobs/find request
- **[medium]** `app/routers/jobs.py:757` — describe_job stores the full posting text (which contains the real pay) but nothing re-evaluates salary after enrichment
- **[low]** `jobflow/app/routers/applications.py:24` — Daily/platform cap and free-taste gate are read-then-insert with no atomicity; two writers can overshoot by one

## Ещё не верифицировано из отчёта 09-19

Остаток 25-строчного списка (кроме разобранных выше) по-прежнему гипотезы:
зомби-heartbeat `activity.py:49`, `/campaign/queue` голодание tap, дата-поля филлера,
Stop fail-open `ping.js:109` (**верифицирован вручную 09-25, см. STATUS**), режим в 3 местах,
сон SW `content.js:5133`, `all_links` без order, legacy price id в billing.
