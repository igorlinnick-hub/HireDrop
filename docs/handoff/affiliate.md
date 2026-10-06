# Affiliate (аффилиатка)

Обновлено: 2026-10-06 (утро) · ветка: main, всё смержено (#278/#282 web, #350/#364 backend)

## Состояние

- **Денежная цепочка доказана вживую (09-28):** `?ref=igor` → отдельный аккаунт `+buyer1` → оплата $12 → комиссия $3.60 → рефанд → `reversed`. Сверка: `jobflow/scripts/affiliate_live_test.py verify igor` (все PASS).
- **Регистрация партнёра доказана (09-25, web 86fbf2d):** намерение едет в `user_metadata.affiliate_intent`. Повторно на проде 10-03 (после перекраски #250/#254): главная → «Get your link» → `/signup?affiliate=1` → анкета.
- **Публичный вход на лендинге:** блок `AffiliateBand` на главной, Drop из блока убран (закрывал лица). Web #250, #254.
- **Stripe Connect-онбординг партнёра доказан вживую (10-06):** код `igor` → живой Express-аккаунт `acct_1UNOyUFEg9qHJX0r`. Stripe: `payouts_enabled=True`, `transfers=active`, `currently_due=[]`. База: `affiliates.payouts_enabled=True`. Флаг перевёл вебхук `account.updated`, руками его не ставили.
- **Не доказано:** настоящий `Transfer` партнёру. Баланс платформы в живом Stripe −$0.73 (10-05), перевод не пройдёт, пока нет оплат. Выплаты из выручки, пополнять не нужно.

## Тест-аккаунт `+aff1`

- Владелец кода `igor` = `hacker987602+aff1@gmail.com`. Логин/пароль: `AFF1_EMAIL` / `AFF1_PASSWORD` в `jobflow/.env` (задан 10-06).
- Вход в 10-05 не прошёл: Safari предлагает только `hacker987602@` и `igor.linnick@`.
- Сброс пароля для `+aff1` запрошен 10-06 02:44:59 UTC. **Письмо ДОШЛО** в 02:45:00: «Reset your HireDrop password» от `support@hiredrop.io`, INBOX ящика **hacker987602@gmail.com**. Ссылка живёт 1 час, до ~03:45 UTC; дальше нужен новый запрос.
- Ложная тревога «письмо не пришло» случилась потому, что искали в ящике `igor` email-MCP, а это **igor.linnick@**. Plus-адреса `hacker987602+…` читаются только Gmail-коннектором claude.ai (`mcp__claude_ai_Gmail__search_threads`, `to:hacker987602+aff1@gmail.com`).

## Закрыто (10-06)

- ~~Сброс не доходит, потому что `RESEND_FROM_EMAIL` не задана~~. Сброс шлёт бэкенд через Resend (`app/routers/auth.py:120` → `modules/email_sender.py`). Пришёл с `support@hiredrop.io`, а не с песочницы `onboarding@resend.dev`, значит переменная в Railway задана и домен проверен. `SECURITY_CHECKLIST.md:124` можно отметить.

- **Возврат из Stripe молчал (найдено 10-06):** `?connect=return` никто не читал, «Payouts connected» обновлялся ниже первого экрана. Web #278 (`0a38ede`, смержен): `ConnectReturnBanner` наверху `AffiliateView`. Возможные состояния: «You're all set — payouts are on», «Confirming with Stripe…» с `router.refresh()` 5×3 с, «link expired». Проверено локально (`next dev`) под `+aff1` в обеих темах. **На проде ещё НЕТ**: Vercel упёрся в лимит сборок на 24 ч; после сброса проверить, что main выкатился.

- **Выплаты по расписанию: ✅ с 10-06.** Railway-сервис `affiliate-payouts` (id `326818da…`), cron `0 15 * * *` UTC, `python scripts/run_affiliate_payouts.py run`, перезапусков нет (NEVER). Переменные — ссылки `${{web.…}}`: SUPABASE_URL, SUPABASE_SERVICE_KEY, STRIPE_SECRET_KEY, SENTRY_DSN. Пробный запуск в боевой среде 04:00 UTC 10-06: `payout run done: 0 paid ($0.00), 0 failed…`. Скрипт с #350: итоговая строка в каждом запуске, exit 1 при сбое перевода. ✅ **Репо подключено 10-06 06:59 UTC**: source = `igorlinnick-hub/HireDrop`@main, сервис сам выкатился на `9a8ce1f` вместе с web. Как подключили: проектный токен `serviceConnect` не может (Not Authorized), а сессия аккаунта может. Своё окно Chrome → railway.com → «Continue with GitHub» (сессия GitHub Игоря) → GraphQL `serviceConnect` через XHR со страницы (`withCredentials`). Railway в Chrome теперь залогинен, этим же путём делаются другие действия уровня аккаунта.
- Почему пропустили (#308, 10-01): в docstring было «daily job», в чеклисте после деплоя был только вебхук. Тесты проверяют счёт, а не запуск. Prod-sweep ловит таблицы, которые перестали пополняться, а `payouts` не пополнялась никогда. Строка в `SAAS_PLAYBOOK.md` §4.
- **Возвраты: РЕШЕНО 10-06, их нет.** Решение Игоря: только отмена подписки. Web #282 (`f1f41d2`): Terms §4 говорит, что платежи не возвращаются, после отмены доступ до конца оплаченного периода; исключение одно — наша ошибка списания (двойной чардж). Это же написано до оплаты (FreeTastePaywall, StepPlan), партнёрские тексты говорят про чарджбэки. Остаётся спор через банк: backend #364 (`9a8ce1f`) `charge.dispute.created` → `reverse_for_invoice`. Счёт ищем через `InvoicePayment.list(payment=payment_intent)`: вебхуки идут на версии API 2023-08-16, а SDK 15.3 = `2026-06-24.dahlia`, где у Charge нет `invoice`. На живом платеже проверено: `pi_3UKlMc…` → `in_1UKlMc…`. ✅ Боевой вебхук `we_1UCSOX…` подписан на `charge.dispute.created` (10-06, Игорь, curl по одному эндпоинту). Полный `stripe_bootstrap.py` ради одного события НЕ запускать: он ещё трогает цены и переливает переменные в Railway.
- Правило выплаты (код): комиссия `accrued` старше 30 дней + сумма ≥ $25 + Connect `payouts_enabled`. `--force` снимает только минимум $25, **30 дней НЕ снимает** (cutoff безусловный). Затем Stripe переводит на банк партнёра ежедневно с задержкой 2 дня.
- Сейчас у `igor`: одна комиссия $3.60 `reversed`, к выплате $0. `--dry-run` при пустой очереди молчит (exit 0, ни строки): по выводу не отличить «нечего платить» от «не работает». Баланс платформы −$0.73.

- **Перевод партнёру доказан в песочнице (10-06) ✅.** Песочница `acct_1NxZyCF5J5iTc6bi`, тестовый Express-партнёр `acct_1ULvY5FMt3FtYjtC` прошёл анкету (тестовые данные, Stripe Test Bank). `Transfer` $3.60 с параметрами скрипта (destination, description, `idempotency_key`) → `tr_1UNRLtF5J5iTc6bi7TJfaatV`. Повтор с тем же ключом вернул ТОТ ЖЕ перевод (защита от двойной выплаты). Баланс партнёра $3.60 available → payout `po_1UNRM3FMt3FtYjtCbSa09QiU` = `paid`. Наш учёт (`payouts`/`commissions`) в песочнице не проверить, база одна, боевая. Его закрывают 11 юнит-тестов и боевой cron-прогон. Боевой перевод ждёт первую настоящую оплату + 30 дней. Stripe CLI = только песочница, правило `Bash(stripe:*)` в local settings.

## Следующий шаг

Модель: **Opus**.
1. ~~Подписка вебхука на чарджбэки~~ ✅ 10-06.
2. ~~Политика возвратов~~ ✅ #282. ~~Connect Repo~~ ✅.
3. После сброса лимита Vercel (~03:40 UTC 10-07): убедиться, что на проде web #278 (баннер) и #282 (Terms «non-refundable»).
4. ✅ Первый плановый cron 10-06 15:00:34 UTC (код из репо, деплой dd53183): `payout run done: 0 paid ($0.00), 0 failed, 0 resumed, 0 commission(s) past the 30-day window`. Пуш в main передеплоивает сервис, но запуска не вызывает, запуск только по cron.
5. ✅ **Сквозной прогон выплаты на песочных деньгах (10-06, #370):** `.venv/bin/python scripts/affiliate_payout_e2e.py`. Временные партнёр и покупатель + комиссия $30 от −31 дня → боевой `cmd_run` с тестовым ключом → 13/13 PASS: payout `completed` + `stripe_connect`, комиссия `paid_out` и привязана, Transfer `tr_1UNfFVF5J5iTc6biEyic4Gvp` → `acct_1ULvY5…`, повторный запуск ничего не платит. Пользователи удалены, строки ушли каскадом. Ключ `STRIPE_TEST_KEY` (песочница) в `jobflow/.env`. Боевой ключ скрипт отвергает; если в базе есть настоящие комиссии к выплате или висящие выплаты, не стартует. Баланс песочницы пополняется `stripe charges create --amount 5000 --currency usd --source tok_bypassPending`.
6. **Живой тест сегодня (Игорь отложил 10-06):** Игорь платит $12 с `+buyer1` (подписки нет, привязка к `igor` осталась с 09-28 → комиссия $3.60 начислится). Claude вручную создаёт `Transfer` $3.60 на `acct_1UNOyU…` с `source_transaction=<charge>`: баланс −$0.73, без привязки к платежу Stripe перевод не даст. Цена ~$1 комиссий. Скрипт так не проверяется (30 дней безусловно). Сессию лучше вести в своём окне Chrome: там уже есть чей-то вход на hiredrop.io, сначала выяснить чей.
7. Боевой перевод: первая настоящая оплата по `?ref=igor` + 30 дней + сумма ≥ $25 → cron переводит сам. Проверить `payouts`/`commissions` и Stripe.

## Файлы (10-06)

backend: `scripts/run_affiliate_payouts.py`, `tests/test_run_affiliate_payouts.py` (#350). web: `components/dashboard/AffiliateView.tsx` (#278). Корень: `SAAS_PLAYBOOK.md` §4. Railway: сервис `affiliate-payouts`. `jobflow/.env`: `AFF1_EMAIL`/`AFF1_PASSWORD`. Local settings: `Bash(stripe:*)`.

## Файлы (10-03…10-06)

web: `components/landing/AffiliateBand.tsx`, `components/affiliate/AffiliateHero.tsx`, `components/landing/DropCameo.tsx`, `app/page.tsx`, `app/login/page.tsx`, `app/globals.css` (+ `.hd-auth`), `components/auth/AuthLayout.tsx`, `components/illustrations/AutoFillDemo.tsx`.
Конкретные PR: #250, #251, #254.
