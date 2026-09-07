# Billing (касса Stripe)

Обновлено: 2026-09-06 · ветка: main

## Состояние

Касса живая и **проверена реальными деньгами end-to-end** (09-06, $9 картой Игоря):
checkout → оплата → webhook → `tier=pro` → «Current plan: Pro» → портал отмены.

Аккаунт Stripe = Hello Systems LLC (single-member, HI), payout на личный BofA weekly,
Radar Lite, Stripe Tax и Climate выключены осознанно. Продукт «HireDrop Pro», два Price:
`hiredrop_pro_weekly` $9 и `hiredrop_pro_monthly` $29 — суммы приходят из
`app/billing_config.py`, это единственный источник правды, витрины их цитируют.

Вся установка — одна идемпотентная команда: `python scripts/stripe_bootstrap.py ship`
(prices + webhook + `.env` + Railway + проверка прода), `… status` — read-only срез.
Проверка живости прода без денег: `POST /api/v1/billing/webhook` с мусором → **400**
(«Invalid signature» = настроено); **503** = ключей нет.

Политика: **возвратов клиентам нет**. Отмена = `cancel_at_period_end`, доступ до конца
оплаченного периода, потом `free` по событию `customer.subscription.deleted`. Рефанд —
ручное исключение (двойное списание, простой), не политика. Правило в `SAAS_PLAYBOOK.md` §2.

## Последний заход

- Живой платёж снял два бага, которых **не видели 19 зелёных тестов на моках**: #145
  (stripe-python v15 отдаёт StripeObject, не dict → `.get()` → webhook 500 на первом
  реальном платеже) и #147 (`current_period_end` уехал из корня подписки в `items.data[]`
  → грант писал NULL-срок → `get_tier` fail-closed возвращал ПЛАТЯЩЕГО во free).
- Тесты переписаны на реальные SDK-объекты + регрессия на items-fallback: 21/21.
- Подписка Игоря отменена через API; refund $9 решили не делать — Stripe не возвращает
  комиссию ($9 брутто / $0.53 / $8.47 нетто), возврат стоит те же $0.53 и вешает запись
  о рефанде на историю свежего мерчант-аккаунта.
- Граница зон: биллинг/Stripe/webhook-код — этот лейн; платформы, движок подачи и мониторы
  прода — продукт-лейн, кассовые алерты он пересылает, сам не чинит.

## Сломано / не доделано

- **`customer.subscription.deleted` ни разу не отрабатывал на живом событии** — единственный
  непроверенный кусок денежного пути. Проверится сам 13.09 (см. ниже).
- Дашборд молчит после успешной оплаты: возврат идёт на `/dashboard?checkout=success`, UI
  не реагирует ничем. Зона веб-апа, деньги не блокирует.
- Грабли окружения: в `.env` легко получить ДВЕ строки `STRIPE_SECRET_KEY` (скрипты берут
  последнюю → 401 на живом ключе). Классификатор Claude Code блокирует refund, извлечение
  `sk_live` из браузера и запись `ADMIN_EMAILS` — это ручное у Игоря. Deep-link'и в вебхуки
  дашборда Stripe редиректят на главную; переотправка события делается API:
  `POST /v1/events/<evt>/retry` с `webhook_endpoint=<we_>`.

## Следующий шаг

13.09 (напоминание у Игоря в календаре) проверить downgrade: `supabase db query --linked
"select subscription_tier from profiles where stripe_subscription_id='sub_1UCXUVF5J5iTc6bijAS0H8cP'"`
→ ждём `free`. Остался `pro` — downgrade сломан, любой отменившийся пользуется платным даром.
