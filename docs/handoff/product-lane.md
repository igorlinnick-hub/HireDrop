# product-lane — бэкенд, дашборд-логика, релизы

Обновлено: 2026-09-05 · ветка: main

## Состояние

- **Движок форм: 83.5% детерминированно / 16.2% AI / 0.3% blank** (замер `python
  scripts/form_coverage.py`, 320 реальных GH-форм). Профиль знает работодателя (#137),
  засеивается из резюме (#139), расширение заполняет (#138, #140).
- **1.7.5 на ревью CWS** (залита через API, `cws_publish.py status` → crxVersion 1.7.5).
  Юзеры на сторовой **1.4.4** — и она работает: живой юзер jay\*\*\* подал 31 заявку за 4 дня.
  Контракт совместимости: бэкенд не ломает ручки, которые зовут старые версии
  (поэтому живы `/connections`), миграции только аддитивные (`ADD COLUMN IF NOT EXISTS`).
- **Биллинг-бэкенд готов и дремлет**: `/billing/{checkout,portal,webhook}`
  (app/routers/billing.py), планы Weekly $9 / Monthly $29 → tier `pro`
  (app/billing_config.py). Отдаёт 503, пока нет `STRIPE_SECRET_KEY`.
- **Миграции применяет сессия сама**: `supabase link --project-ref msxjcjzmfruizbgkssxo
  --yes` (во временной папке) → `supabase db query --linked -f migrations/<файл>.sql` →
  `notify pgrst, 'reload schema'` → REST-проверка колонки. Игорь не нужен.
- 192 теста (+19 биллинг #141, +10 ops-watch #142), ruff чист, CI настоящий. Ext синкнут
  на Рабочий стол.
- **Observability закрыта на первый круг (#142)**: `app/ops_watch.py` — 5xx-burst
  (≥5/10мин) и молчаливый ноль скрейпера (0 подряд ≥5 раз, сигнатура #113); stderr всегда,
  email при `ALERT_EMAIL`; смотреть `GET /tools/ops-scan` (admin). Вид per-worker.

## Последний заход (09-05)

- #137+#115+#138: цепочка `current_employer`/`current_title` целиком — бэкенд, миграция
  в прод, карточка Settings, заполнение в content.js. Blank 21 → 9 на замере.
- #139: засев employer/title из `experience[0]` уже оплаченного парса резюме — только в
  ПУСТЫЕ поля (ввод юзера всегда побеждает). Повод: поле в Settings ≠ данные — адрес за
  три недели заполнил 1 из 28.
- #140: «have you (ever|previously) been employed by X» + «how did you first learn» —
  30 вопросов из AI в детерминированные. Гард: «employed by X» = про эту компанию → No;
  «employed in the <industry>» = про кандидата → LLM.
- Метод закреплён: новое правило проверять прогоном ВСЕЙ if/else-цепочки по 320 схемам
  с печатью списка пойманных лейблов (так нашли capaCITY/reLOCATION и юр-yes/no).
- Разбор 28 профилей: 15 тестовых + 13 органики; jay\*\*\* — первый настоящий юзер.

## Сломано / не доделано

- **КАССА (текущая стадия)**: jay\*\*\* в ~9 заявках от free-лимита (40 lifetime), за
  пейволлом 503. Ждём 3 действий Игоря: Stripe Activate (Individual + личный банк),
  `STRIPE_SECRET_KEY` → `jobflow/.env`, `railway login`.
- PR #53 (промо elite→pro, 2 строки) mergeable с 25.07 — ждёт слова Игоря.
- `elite` мёртв, но промо раздаёт его по умолчанию (до решения по #53).
- Троттлинг фонового окна 5-6 мин/форма (цель <90с) — только с ре-верификацией живьём.
- `ALERT_EMAIL` ждёт `railway login` — до этого алерты ops-watch/#125 видны только в
  Railway-логе.

## Следующий шаг

Как только `STRIPE_SECRET_KEY` лежит в `jobflow/.env` — **одна команда**:
`python scripts/stripe_bootstrap.py ship` (#141). Она сама: Prices по lookup_key →
webhook + signing secret → upsert `.env` → `railway variables --set` (если CLI
разлогинен — печатает команды) → verify (prod webhook 400 = касса поднята, 503 = нет).
Read-only проверка в любой момент: `python scripts/stripe_bootstrap.py status`.
Денежный путь покрыт тестами (`tests/test_billing.py`, 19 шт).
