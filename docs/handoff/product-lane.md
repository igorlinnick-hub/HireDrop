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

## Закрыто 09-05 ночью (не переделывать!)

- **КАССА LIVE**: Stripe активирован (Hello Systems LLC), `stripe_bootstrap.py ship`
  создал Prices+webhook, 5 переменных в Railway, prod probe = 400. Проверка:
  `python scripts/stripe_bootstrap.py status`. Кассой владеет Stripe-сессия Игоря —
  продукт-лейн её НЕ трогает.
- **PR #53 смержен** (Игорь сказал «мержим»): промо grant'ит `pro`; легаси premium/elite
  схлопываются в `pro` внутри `get_tier`. Дыра «elite бесплатно 75/день» закрыта.
- **`ALERT_EMAIL` стоит в Railway** — stall-watch #125 и ops-watch #142 шлют почту.
- **Ветки почищены 49 → 5**: остались `main` + 4 с потенциально живой работой:
  `feat/cold-email-outreach` (Phase 1, ждёт Gmail OAuth), `platform/google-jobs` (09-03),
  `feat/gh-test-bundle` и `feat/tap-all-platforms` (июльский tap/pool — в main НЕТ
  `poolDoneUrls`; судить ext-сессии, не удалять вслепую).
- Тест-фикс #143: биллинг-тесты больше не зависят от того, поднята ли касса в `.env`.

## Сломано / не доделано

- **Checkout не прогнан живьём**: prod отвечает 400 (configured), но саму ссылку
  checkout → оплата → webhook → тир никто e2e не проходил. Пейволл-UI у jobflow-b1.
- Троттлинг фонового окна 5-6 мин/форма (цель <90с) — замер у ext-сессии
  (`ext/zr-live-verify`), фиксы только с ре-верификацией живьём.
- Sentry нет (SAAS_PLAYBOOK §4) — единственная открытая строка наблюдаемости.

## Аудит пути до первого доллара (09-05 ночь) — бэкенд-сторона ПРОВЕРЕНА

- Гейт: `check_can_apply` отдаёт lifetime-отказ РАНЬШЕ дневного (юзер видит «subscribe»,
  не «come back tomorrow»); `/applications/save` на отказ отвечает 429 c free_used/free_limit.
- **1.4.4 (сторовая jay) защиту ИМЕЕТ** — проверено по git-снапшоту `2e9f2b1`: pre-start
  гейт исчерпанного free, стоп на `free_used >= free_limit` после сейва, стоп по 429.
  Невидимой траты не будет.
- **Live Stripe создаёт checkout-сессии обоих планов** (прямой вызов с теми же параметрами,
  что у `/billing/checkout`): $9/нед и $29/мес, `livemode=True`, url отдан. Сессии-пробы
  истекают сами, денег не двигали.
- НЕ проверено человеком: реальная оплата картой → webhook → `subscription_tier=pro`.
  Это случится первым живым платежом (webhook-путь покрыт тестами #141).

## Следующий шаг

Бэкенд-часть критического пути закрыта. Бутылочное горлышко теперь НЕ в этом лейне:
пейволл-UI/checkout-поток — jobflow-b1 (ждёт Enter), улика ZR — ext-сессия. В этом лейне
дальше по ценности: (1) Sentry на бэкенд (последняя открытая строка SAAS_PLAYBOOK §4),
(2) после первого живого платежа — сверить webhook-грант по Railway-логу и profiles.
