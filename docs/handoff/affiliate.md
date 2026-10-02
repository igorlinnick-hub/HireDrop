# Affiliate (аффилиатка)

Обновлено: 2026-10-01 · ветка: feat/affiliate-connect-payouts (worktree `.wt-affiliate-connect`)

## Состояние

Автовыплаты на Stripe Connect построены, прошли `blast-radius`-ревью, и главная дыра
(`account.updated` не доходил до вебхука) закрыта и **проверена живьём на песочнице**.
Миграции применены на боевой Supabase. Код: `POST /affiliate/payouts/connect`
(Express-аккаунт + AccountLink, IDOR закрыт), вебхук `account.updated` в `billing.py`
(принимает подпись ДВУХ эндпоинтов), `scripts/run_affiliate_payouts.py` (ежедневная
задача, пока ручная). Сайт: кнопка Connect + история выплат — незакоммиченные правки в
основном чекауте `jobflow-website`. Сьют, `ruff` зелёные.

## Последний заход

- **Connect-вебхук.** Обычный эндпоинт Stripe получает события только своего аккаунта
  (подтверждено докстрингом SDK `WebhookEndpoint.create`), поэтому `account.updated`
  партнёров не приходил бы никогда и `payouts_enabled` не включился бы ни у кого.
  Сделано: `STRIPE_CONNECT_WEBHOOK_SECRET` в `config.py`; `billing.py` проверяет подпись
  сначала основным секретом, потом Connect-секретом; подписанное Connect-секретом
  принимается только для `CONNECT_EVENTS = {account.updated}`, остальное — `ignored`
  (не даёт тир и не начисляет комиссию). `stripe_bootstrap.py ship` создаёт второй
  эндпоинт на тот же URL с `connect=true` + `metadata[role]=affiliate-connect` (флага
  connect в объекте API нет — различаем по metadata) и пишет секрет в `.env`/Railway.
- 5 тестов с настоящей HMAC-подписью Stripe (не мок `construct_event`) в
  `tests/test_affiliate_connect.py`.
- **Живая проверка (песочница hiredrop.io, `acct_1NxZyCF5J5iTc6bi`):** локальный бэкенд
  из воркгрива без живых Stripe-ключей + `stripe listen --forward-connect-to` → правка
  метаданных тестового партнёра `acct_1ULvY5FMt3FtYjtC` → `connect account.updated` →
  **200**. На боевой базе это no-op: строки с таким `stripe_account_id` нет.
- `run_affiliate_payouts.py run --dry-run` на боевой базе — пусто, без ошибок (живых
  оплат по рефссылкам ещё не было, платить некому).
- Раньше в этой же ветке (ревью): регресс `clicks` в `affiliate_stats()`, два пути
  двойной выплаты (`affiliate_admin.py payout` и «payable» на борде), гонка в резюме
  `pending`-выплаты — всё исправлено с тестами.
- Stripe CLI: `~/.local/bin/stripe`, залогинен ТОЛЬКО в песочницу. Правило
  `Bash(~/.local/bin/stripe:*)` добавлено Игорем. Создание вебхук-эндпоинтов через CLI
  серверный классификатор блокирует даже в песочнице.

## Сломано / не доделано

- **Живой Connect-эндпоинт ещё не создан.** После мержа и деплоя нужен
  `python scripts/stripe_bootstrap.py ship` из основного `jobflow/` (живой ключ):
  создаст второй эндпоинт и положит `STRIPE_CONNECT_WEBHOOK_SECRET` в `.env` и Railway.
  Без него автовыплата на проде не включится ни у кого.
- Настоящий `Transfer` не прогнан: нужен баланс песочницы + партнёр, прошедший
  Express-онбординг (тестовые данные в форме Stripe — руками Игоря). Сам вызов
  стандартный, идемпотентность покрыта тестами.
- `run_affiliate_payouts.py` нигде не запланирован — оставлен ручным до первых выплат.
- Рефанд уже выплаченной комиссии: лог + руки (`Transfer.create_reversal` не автоматизирован).
- Сайт (`jobflow-website`: `lib/api.ts`, `AffiliateView.tsx`, `affiliate/page.tsx`) не
  закоммичен — коммитить только эти три файла, рядом чужой WIP про маскота.

## Следующий шаг

PR бэкенда → мерж → `stripe_bootstrap.py ship` (живой Connect-эндпоинт) → PR сайта.
Потом при желании: Игорь проходит тестовый Express-онбординг по ссылке из песочницы →
настоящий тестовый `Transfer`. Модель: Opus.
