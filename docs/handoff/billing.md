# billing (касса Stripe)

Обновлено: 2026-09-26 · ветка: main (#261, #265)

## Состояние

Касса проверена **реальными деньгами end-to-end** (09-06, $9 картой Игоря): checkout → оплата →
webhook → `tier=pro` → «Current plan: Pro» → портал отмены. Аккаунт = Hello Systems LLC (HI),
payout на BofA weekly, Radar Lite, Stripe Tax и Climate выключены осознанно. Один продукт
«HireDrop Pro», два Price ($12/нед, $39/мес); суммы живут в `app/billing_config.py` — витрины их
цитируют. Вся установка: `python scripts/stripe_bootstrap.py ship` (`… status` — read-only).
Живость прода без денег: `POST /api/v1/billing/webhook` с мусором → **400** («Invalid signature»
= настроено); **503** = ключей нет.

Политика: **возвратов нет**. Отмена = `cancel_at_period_end`, доступ до конца периода, потом
`free` по `customer.subscription.deleted`. Рефанд — ручное исключение, не политика
(`SAAS_PLAYBOOK.md` §2).

**Правило вебхука (#261): строка в `stripe_events` — ПРЕТЕНЗИЯ, не расписка.** Было:
`mark_event_processed` над `try`, `except` отдавал 200 → любой сбой съедал событие навсегда
(Stripe не повторяет 200, ручной resend уходит в `duplicate`), платящий сидел во `free` до правки
БД руками. Теперь `claim_event` → работа → при сбое `release_event` + **5xx**. Повтор безопасен:
`grant` перезаписывает, комиссии upsert по `stripe_invoice_id`. Следствия того же фикса:
`invoice.paid` раньше `checkout.session.completed` больше не теряет комиссию ПЕРВОГО счёта
(передоставка 24 ч, `UNRESOLVED_RETRY_WINDOW_SECONDS`, после окна — громкий лог), и второй
checkout не плодит второго customer'а: живая подписка → **409** в портал (было: два списания в
месяц и ни одного отменяемого). «Не смогли спросить Stripe» ≠ «подписан».

**Legacy Price (#265):** Stripe Price неизменяем, репрайс плодит новые id, а в env лежит текущая
пара — подписчик со старой цены не грантился НИКОГДА, платил и уезжал во `free` в конце периода.
Теперь неизвестная цена сверяется по ПРОДУКТУ: наш → грант `pro` + лог с id для env; чужой или
Stripe промолчал → ничего.

## Сломано / не доделано

- **Живым платежом #261/#265 не проверены.** Моки не ловили и класс 09-06 (#145 StripeObject не
  dict → 500; #147 `current_period_end` уехал в `items.data[]` → грант писал NULL и `get_tier`
  возвращал платящего во free). Доказательство даст следующий реальный счёт; в логах Railway
  искать `[billing]` (release / giving up / COULD NOT RELEASE).
- `customer.subscription.deleted` ни разу не отрабатывал на живом событии.
- Дашборд молчит после успешной оплаты (`/dashboard?checkout=success` ничем не реагирует) — зона
  веб-апа, деньги не блокирует.
- Грабли окружения: в `.env` легко получить ДВЕ строки `STRIPE_SECRET_KEY` (скрипты берут
  последнюю → 401). Refund, извлечение `sk_live` и запись `ADMIN_EMAILS` классификатор Claude
  блокирует — это ручное у Игоря. Переотправка события: `POST /v1/events/<evt>/retry` с
  `webhook_endpoint=<we_>` (deep-link'и дашборда Stripe редиректят на главную).

## Следующий шаг

Проверить downgrade (напоминание у Игоря было на 13.09): `supabase db query --linked "select
subscription_tier from profiles where stripe_subscription_id='sub_1UCXUVF5J5iTc6bijAS0H8cP'"` →
ждём `free`. Остался `pro` — downgrade сломан, любой отменившийся пользуется платным даром.
