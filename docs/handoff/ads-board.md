# Ads board (платная реклама: CAPI, траты, вкладка Ads)

Обновлено: 2026-09-28 · ветка `feat/ads-board` (бэкенд). Параллельно: website-PR с пикселем и
новыми ключами `profiles.attribution`.

## Состояние

Тест Meta (FB+IG) + Google Search, $300–500/мес. Построено и покрыто тестами, **в проде ещё
не включено**: миграция `ad_spend` не применена, env на Railway не заданы. До этого момента
вкладка Ads показывает «—» с причиной вместо трат, а CAPI — полный no-op.

- **Meta CAPI** (`app/ads/meta_capi.py`): `StartTrial` `act_<user_id>` на ПЕРВУЮ заявку
  (`/applications/save`), `Purchase` `pay_<invoice_id>` на каждый `invoice.paid` с деньгами
  (после начисления аффилиатке). Пиксель сайта шлёт `CompleteRegistration` `reg_<user_id>`.
  Только Meta-атрибутированные и без `ads_optout`; без `ua` не шлём (Meta требует
  `client_user_agent` для website-событий). Поток-демон, 5с таймаут, никогда не бросает.
- **Траты** → таблица `ad_spend` (platform, date, ad_id). Meta тянет сама вкладка (раз в час,
  7 дней) или CLI; Google присылает Ads Script; остальное — `ads_spend.py add`.
- **Вкладка Ads** (`_section_ads` в `app/routers/admin.py`, после Funnel): Spend, Budget used,
  Paid-ad signups, Cost/signup, Activated, Cost/activation, Paying, CAC, ROAS; таблицы By channel
  (с органикой), By ad (с вердиктом), Unmatched, Spend sources.
- **Funnel починен**: «Connected extension» считает ЛЮДЕЙ (было 15/17 = 88% по строкам ключей,
  стало 2/17 = 11.8%), «Started a campaign» (всегда 0 — `started_at` чистится на Stop) заменён на
  «Sent first application» (2/17). Метрика `campaigns_started` → `first_application`;
  `activation_rate` теперь = signup → первая заявка.

## Решения

- **«Платил»** = succeeded Stripe charge за вычетом возвратов на customer, привязанном к юзеру
  (`profiles.stripe_customer_id`, пишется на `checkout.session.completed`). НЕ
  `subscription_tier` (промо пишет туда же, истёкшая подписка = free). Выручка на юзера — из того
  же чтения Stripe (общий `_paid_charges` с секцией Revenue). Если Stripe не читается —
  fallback: привязанный customer = платил, выручка `None`.
- **Каналы**: `meta_paid` = source ∈ {facebook, fb, instagram, ig, meta} И paid-medium;
  `google_paid` = gclid/gbraid/wbraid ИЛИ google + paid-medium; иначе `_source_of`. fbclid сам по
  себе НЕ платный. Код — `app/ads/attribution.py`.
- **Manual-траты** матчатся на сигнапы по `account_id` = utm_source (`--channel reddit`).
- **Нет источника = `None` с причиной**, не 0. Meta настроена, но синк падает и строк нет —
  тоже `None`. Google «подключён», когда скрипт хоть раз прислал строки.
- **Вердикт** (`app/ads/verdict.py`, порядок важен): `wait` (spend < $20 и impr < 2000) →
  `scale` (≥1 платящий с CAC ≤ потолка, или ≥2 активированных с cost/activation ≤ потолок/2) →
  `kill` ($30+ без сигнапов; ≥2000 impr с CTR < 0.7%; $60+ с сигнапами, но 0 активаций) →
  `keep`. Scale раньше kill осознанно: деньги важнее прокси (CTR). Потолок
  `ADS_CAC_CEILING_USD` = 39 (месяц подписки).

## Env на Railway

| Переменная | Зачем |
|---|---|
| `META_PIXEL_ID`, `META_CAPI_TOKEN` | CAPI (токен из Events Manager → Settings → Conversions API) |
| `META_TEST_EVENT_CODE` | опционально: события в Test Events, убрать после проверки |
| `META_ADS_TOKEN`, `META_AD_ACCOUNT_ID` | Insights (system-user токен с `ads_read`; id с `act_` или без) |
| `META_GRAPH_VERSION` | опционально, по умолчанию `v24.0` |
| `ADS_INGEST_TOKEN` | секрет для Google Ads Script (`openssl rand -hex 24`) |
| `ADS_MONTHLY_BUDGET_USD` | по умолчанию 500 |
| `ADS_CAC_CEILING_USD` | по умолчанию 39 |

## UTM-конвенция (ключ связи трат и сигнапов = utm_content = id объявления)

- Meta, URL parameters: `utm_source=facebook&utm_medium=paid_social&utm_campaign={{campaign.id}}&utm_content={{ad.id}}&utm_term={{adset.id}}`
- Google, tracking template: `{lpurl}?utm_source=google&utm_medium=cpc&utm_campaign={campaignid}&utm_content={creative}&utm_term={keyword}` + auto-tagging (gclid)

## Как подключить

1. Миграция: `migrations/2026-09-28_ad_spend.sql` (оркестратор) → `notify pgrst, 'reload schema'`.
2. Meta: env выше → открыть борд (синк сам) или `scripts/ads_spend.py sync-meta --days 7`;
   CAPI проверить через `META_TEST_EVENT_CODE` в Events Manager → Test Events.
3. Google: `ADS_INGEST_TOKEN` на Railway → вставить `scripts/google_ads_spend.js` в Google Ads →
   Tools → Scripts, вписать токен, Preview (лог «HTTP 200»), расписание Daily.
4. `scripts/ads_spend.py status` — что подключено и когда синкалось.

## Открытые вопросы

- Night-shift (`scripts/night_shift/executor.py`) пишет заявки мимо `/applications/save` —
  первая заявка через него `StartTrial` не шлёт.
- Meta `clicks` = все клики (не link clicks) — CTR-порог 0.7% считается по ним; при желании
  перейти на `inline_link_clicks`.
- Hellometrix-карточка метрики до мержа его ветки `a9914b8` («missing number renders as a dash»)
  рисует `None` как 0; в таблицах `None` уже «—».
- Когорта: траты периода делятся на сигнапы периода; платящие/активация — «к сегодняшнему дню».
