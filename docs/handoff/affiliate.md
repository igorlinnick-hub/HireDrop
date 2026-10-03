# Affiliate (аффилиатка)

Обновлено: 2026-10-03 · ветка: main (всё смержено)

## Состояние

Автовыплаты партнёрам через Stripe Connect **в проде целиком**: бэкенд (HireDrop #308),
живой Connect-вебхук создан (`stripe_bootstrap.py ship`, эндпоинт `we_1ULviPF5J5iTc6bilNRih36M`,
`STRIPE_CONNECT_WEBHOOK_SECRET` в `.env` и Railway), кнопка «Connect payouts» + история выплат
на `/dashboard/affiliate` (web #237). PayPal убран из анкеты и текстов (web #248). Лица в
кружках — реальные Envato-фото (web #246). Живой партнёр ещё ни разу не подключал счёт.

## Последний заход

- `account.updated` с подключённых аккаунтов идёт ТОЛЬКО на эндпоинт `connect=true` со своим
  секретом. `billing.py` проверяет подпись обоими секретами; Connect-секрет принимается лишь
  для `CONNECT_EVENTS = {account.updated}`. Проверено в песочнице: `stripe listen
  --forward-connect-to` → `connect account.updated` → 200.
- `run_affiliate_payouts.py run --dry-run` на боевой базе — чисто, платить некому.
- Stripe CLI `~/.local/bin/stripe` залогинен ТОЛЬКО в песочницу; правило
  `Bash(~/.local/bin/stripe:*)` есть. Изменения ЖИВОГО Stripe (bootstrap ship, вебхуки)
  классификатор режет — запускает Игорь в терминале VS Code (без `!`: в zsh `!` инвертирует
  и пропускает команду после `&&`).
- Лица: оригиналы `brand-visuals/affiliate-people/envato/`, кроп
  `brand-visuals/crop_affiliate_people.py --out jobflow-website/public/people` → `face-<n>.jpg`.
- Аудит скорости/защиты сайта передан сессии техдолга (jobflow-5f) — это ЕЁ лейн, сюда не тянуть.

## Сломано / не доделано

- Не пройден живой онбординг Connect. Код `igor` привязан к `hacker987602+aff1@gmail.com`
  (вход email+пароль), не к `+buyer1` — Игорь был залогинен покупателем и видел анкету.
- Настоящий `Transfer` не прогнан. Купить по своей же ссылке нельзя: реферал на самого себя база
  отклоняет (триггер в `add_affiliates.sql`), окно 30 дней в `run_affiliate_payouts.py` не
  обходится флагом.
- `run_affiliate_payouts.py` не запланирован (ручной до первых выплат).
- Рефанд выплаченной комиссии — лог + руки.
- `app/affiliate/page.tsx:106` — давний eslint `react/no-unescaped-entities` (не наш).

## Следующий шаг

Игорь входит как `hacker987602+aff1@gmail.com` → `/dashboard/affiliate` → «Connect payouts» →
форма Stripe (новый аккаунт, не касса). Проверить: `affiliates.payouts_enabled=true` для кода
`igor`. Потом решение Игоря: проверочная выплата $25 себе (заносим одну проверочную комиссию
старше 30 дней → `run_affiliate_payouts.py run --force`) или ждать живого реферала.
Модель: Opus.
