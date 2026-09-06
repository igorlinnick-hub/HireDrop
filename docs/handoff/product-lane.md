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

## Закрыто 09-06 ночью (живой прогон + первый платёж)

- **КАССА E2E ЖИВЫМИ ДЕНЬГАМИ**: checkout → $9 → webhook → `pro до 09-13`. Два бага
  сняты первым платежом: #145 (v15 StripeObject без `.get()` → 500 на webhook; поймал
  ops-watch первым боевым алертом) и #147 (period_end переехал на item → NULL expiry →
  fail-closed на free; фикс — Stripe-сессия, нашли одновременно). Регрессионные тесты
  на РЕАЛЬНЫХ SDK-объектах. Идемпотентность проверена повторными событиями.
- **ReadError-500 закрыт #148**: постгрест-сессия → HTTP/1.1, keepalive 15с, retries=1
  (грабля: `limits=` httpx.Client игнорируется при своём `transport=`). Тот же корень
  валил `GET /jobs`, `/campaign/status` и stall-watch `list_running`.
- **Живой прогон**: Indeed by-link tap = `applied` В ЗАПИСИ (гэп TAP_INSTANT закрыт,
  память обновлена); Braze GH — форма 28 полей дошла до `/confirmation` (клик Игоря на
  недостающем дропдауне); из этого #146 — правила «talent community → No» (22 hit на
  320 схемах) и «commute → Yes», детерминизм 83.5→83.8%. Ashby «не в очереди» = НЕ баг:
  обе карты поданы 08-05/08-10, дедуп верен (но их jobs.status завис в `approved` —
  косметика). Мёртвый Barika — честный скип.

## Сломано / не доделано

- **Checkout не прогнан живьём**: prod отвечает 400 (configured), но саму ссылку
  checkout → оплата → webhook → тир никто e2e не проходил. Пейволл-UI у jobflow-b1.
- Троттлинг фонового окна 5-6 мин/форма (цель <90с) — замер у ext-сессии
  (`ext/zr-live-verify`), фиксы только с ре-верификацией живьём.
- Sentry нет (SAAS_PLAYBOOK §4) — единственная открытая строка наблюдаемости.
- Ops-заметка по Interview Kit (от 6d): генерация только по явному тапу и только у
  заявок interview/interview_invite; слот берётся ДО вызова Sonnet и возвращается на
  провалах. Делит ОБЩИЙ дневной AI-кап (120) со всеми эндпоинтами — если usage растёт
  не по профилю, смотреть сюда первым; при жалобах grep `[interview_kit] generation
  failed` в Railway-логе.
- Веб-ап мелочи (наблюдения 6d + ночи): удалить мёртвый `ApplicationHistory.tsx`
  (не рендерится, живой список = HistoryView; внутри кнопка бьёт в несуществующий
  относительный URL); `Date.now()` в useMemo HistoryView (ESLint purity); «дашборд
  молчит после оплаты» — баннер на `?checkout=success`; Start-кнопка тапалки видна
  только при пустой колоде; fail-open вердикт старта в QuickActions (класс #98);
  PR #121 (UsageBanner) ждёт Vercel-чека и мержа; jobs.status=approved у поданных
  в августе Ashby-карт.

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
