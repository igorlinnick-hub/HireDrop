# Ads board (платная реклама: пиксель, CAPI, траты, сборка кампании)

Обновлено: 2026-10-05 · ветка: main

## Состояние

- **Meta готова к запуску, кроме токена и видео.** Портфель Hiredrop 2022625988457757, аккаунт
  HireDrop Ads 1672047784439131 (USD, Pacific/Honolulu, **оплата Visa подключена 10-04**), страница
  HireDrop 1430549256789872, пиксель 1113284891046203, домен Verified, CAPI живой (Railway
  `META_PIXEL_ID`/`META_CAPI_TOKEN`/`META_AD_ACCOUNT_ID`; `META_TEST_EVENT_CODE` убран).
- **Датасет (10-04):** ИИ-сбор деталей страниц Off, allow list = `hiredrop.io`, авто-advanced
  matching и автособытия Off. Пиксель живьём: `PageView` только на публичных страницах, на
  /dashboard и /onboarding нет (SPA-утечку закрывает `disablePushState`). `CompleteRegistration` ещё
  ни разу не приходил — новых регистраций с сайта не было.
- **Кампания = спека** `../content-lab/ads/campaigns/meta-r1.json` (Leads → CompleteRegistration,
  EMPLOYMENT, $8/день, US Advantage+, M1–M3 статика 4:5+9:16 включены, V1/V2 видео-слоты выключены).
  Сборщик `scripts/meta_ads.py` (#328 в main): `whoami/plan/build/status/teardown`, всё PAUSED,
  id созданного → `meta-r1.state.json` рядом со спекой. Включение — только Игорь в Ads Manager.
  Ранбук (видео-требования, токен, день запуска): `../content-lab/ads/campaigns/README.md`.
- Решения по вкладке Ads/вердиктам/каналам — без изменений, см. `app/ads/*`, ADS_PLAN §3–6.

## Последний заход

- 10-04/05: проверил пиксель живьём (Safari, своё окно) и Events Manager; написал спеку + сборщик +
  9 тестов (payload'ы: PAUSED, EMPLOYMENT, центы, событие пикселя, без age/gender/ZIP, правила
  плейсментов, UTM). `plan` проверен офлайн; против живого API НЕ гонялся — нет токена.
- Грабли: клики в настройках Meta через osascript режет auto-mode классификатор (читать можно) —
  кликает Игорь. Events Manager в Safari грузится ~40 с, 01.10 не грузился вовсе. Окна Safari
  создавать `make new document` → `set URL of current tab of window id N`.

## Сломано / не доделано

- Нет `META_ADS_TOKEN` (Meta-приложение → system user → токен с `ads_management`+`ads_read`+
  `pages_*`) — без него ни сборки кампании, ни трат на доске.
- Instagram `@hiredrop.io` не привязан к портфелю — IG-показы пойдут от имени страницы.
- Первый `build` может упереться в детали API (Advantage+ audience при EMPLOYMENT, формат
  `asset_customization_rules`) — чинить по ответу Meta, спека/сборщик рассчитаны на перезапуск.
- Night-shift закрыт 10-02 — дыра «StartTrial мимо /applications/save» больше не актуальна.
- Google Ads не начат (ADS_PLAN §2 Google).

## Следующий шаг

Модель: **Opus**. 1) Игорь кладёт токен в буфер → `pbpaste` в `.env` + Railway `META_ADS_TOKEN`
→ `git -C jobflow pull` (сборщик в main) → `meta_ads.py whoami` → `build` → `status`.
2) Видео Игоря → `../content-lab/ads/creatives/R2-video/V<n>_1080x1920.mp4`, копирайт под угол,
`enabled: true`, `build`. 3) Тестовая регистрация с Meta-UTM (ADS_PLAN §7) → Игорь включает кампанию.
