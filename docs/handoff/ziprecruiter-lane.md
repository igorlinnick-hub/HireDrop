# HANDOFF — ZipRecruiter lane (2026-08-15, вечер HST)

> Ветка для НОВОЙ сессии. Igor: «ZR должен работать так же, как Indeed. Кампания должна быть
> автономна (сама переключать платформы), но и сама платформа обязана работать».
> Всё, что ниже — реальные улики из бэкенд-лога + живого DOM, не догадки.

## 0. Что уже сделано сегодня (в проде, ext v1.4.4, синкнуто на ~/Desktop/HireDrop-Ext)
| PR | Что |
|---|---|
| jobflow #100 + web #96 | Hybrid work-setting чип + **радиус впервые доходит до расширения** (раньше терялся) |
| jobflow #101 + web #98 | «Did you mean» автокоррект ключевиков (Haiku, только явные опечатки) |
| web #99, #100 | Salary → один пол «Min pay» + /yr /hr тумблер |
| web #97 | `context_invalidated` → баннер «расширение обновилось — обнови вкладку» |
| jobflow #102, #103 | **Ротация ключевиков**: каждый keyword = свой поиск (бюджет ~24 стр / n, min 4) |
| jobflow #104 | **Failover платформ** (`PLATFORM_EXHAUSTED`): 6 external подряд / кап платформы / ключевики кончились / ZR redirect-loop → свитч indeed↔ziprecruiter, `triedPlatforms`, стоп только когда некуда. **+ фикс харвеста ZR-бэйджа** (первый `.text-brand` в карточке теперь ПУСТОЙ узел → сканирую все кандидаты) |
| CWS | **1.4.4 отправлена на ревью, автопубликация** («Ожидает рассмотрения»). Сделано мной через Safari-осаскрипт (пикер = физический клик + Go-to-folder sheet) |

Сторовский двойник 1.4.0 снесён; на chrome://extensions одна карточка HireDrop **1.4.4** (ID edbhcm…).
Кнопка «Errors» на карточке была и до перезагрузки — накопленный старый лог, не блокер (пакет валиден:
manifest/файлы/`node --check` — всё OK).

## 1. ТЕКУЩАЯ ПРОБЛЕМА (последний прогон 07:36–07:38 UTC, ext 1.4.4 подтверждён пингом)
Ключевики: healthcare marketing / social media manager / project manager / ai engineer. Miami, Hybrid, Full-time, Auto.

**Что видно в логе (newest first):**
```
07:38:29 Skip (title mismatch): New Business Development - Nursing Home… @ Transformational Health
07:38:22 Content script alive on jobs-search/3 — resuming
07:38:01 ⏭️ Skipped (fit 30): Strategic Partner Marketing Manager @ Syndio — B2B SaaS, out of…
07:37:47 ⏭️ Skipped (fit 30): Healthcare Sales Representative @ ChenMed — sales, not marketing
07:37:35 Warmup complete — navigating to ZipRecruiter search
07:36:55 Content script alive on jobseeker/home — resuming     ← это и был скрин «Good Evening»
```
URL окна автоматизации: `jobs-search/4?…&search=healthcare+marketing` (**ротация работает** — один
ключевик, не mashup). Пинг: `campaign_running:false` → кампания уже остановилась сама.

**Диагноз:** ZR по «healthcare marketing» в Miami даёт мало нативных Quick Apply, а те что есть — не
проходят fit-gate (fit 30 < порог) или title-match. Прошла 4 страницы → скорее всего сработал стоп
через `pageOrRotate` / failover (проверить: должна быть строка «switching to Indeed» или «exhausted…
stopping» — в лог-выборке выше её ещё НЕТ, кампания могла упереться до неё). **Не «застряла» —
скипы идут каждые 7–15 с, это штатный темп с humanDelay.**

Экран «Good Evening» = warmup-хоп на homepage (для CF cookie), через 40 с ушёл на search — норма.

## 2. ЧТО ПРОВЕРИТЬ ПЕРВЫМ ДЕЛОМ (новая сессия)
1. Дочитать лог ДО момента остановки: `GET /api/v1/activity?limit=100` (через авторизованную
   вкладку дашборда: токен в cookies `sb-msxjcjzmfruizbgkssxo-auth-token.0/.1`, склеить, снять
   префикс `base64-`, `atob` → `access_token`; XHR из вкладки через osascript `eval(atob(B64))`.
   Готовый скрипт: scratchpad `extping.js` / `act.js`). Ищем: `PLATFORM_EXHAUSTED`-строки
   («exhausted … switching to Indeed» / «…stopping the campaign»), «All keywords searched»,
   «Found N Quick Apply jobs», APPLY DIAG.
2. Если failover НЕ сработал (кампания стопнулась без строки свитча) — баг в новом коде
   `background.js case "PLATFORM_EXHAUSTED"` (jobflow #104) или в `pageOrRotate` — это
   ГЛАВНЫЙ подозреваемый, код НЕ live-верифицирован.
3. Если сработал и ушёл на Indeed — смотреть, что было на Indeed.

## 3. ГИПОТЕЗЫ ПО ПОРЯДКУ ВЕРОЯТНОСТИ
- **H1 (высокая):** ZR-выдача по этим ключевикам в Miami объективно бедна нативными вакансиями,
  fit-gate честно режет → нужен failover на Indeed (сделан, но не проверен в бою) — см. §2.
- **H2:** харвест ZR-бэйджа всё ещё мажет на части карточек (фикс #104 сканирует
  `div[class*='bg-badge-brand'] p, .text-brand, p[class*='text-brand']` — проверить на живой
  выдаче через свой Chrome-window + `eval(atob())` DOM-дамп; техника в памяти
  `reference_ext_reload_mechanics` + `project_geo_radius_map`).
- **H3:** fit-threshold для Auto слишком строгий для ZR-микса (35/55/70 по apply_mode) — но это
  фича (ban-safety), не баг; менять только осознанно.
- **H4 (низкая):** hybrid-токен в запросе (`… hybrid`) сужает ZR-выдачу — совет это отмечал; я
  оставил, т.к. у ZR/Indeed нет стабильного work-type param. Быстрый тест: тот же прогон с
  Work setting = Any.

## 4. ЧТО НЕ ДЕЛАТЬ
- Не расследовать «зависание» как краш — паузы 10–20 с между страницами штатные (`humanDelay`).
- Не откатывать ротацию/failover вслепую — они верны офлайн; нужен ЖИВОЙ прогон с чтением лога.
- Не трогать `chrome://extensions` осаскриптом (JS/AX/CDP закрыты Chrome'ом) — только руками Igor.
  Версию проверять пингом (`/extension/ping` → `version`), не «на глаз».
- Не гонять расширение из ДРУГОЙ сессии — Igor: «с той сессией экстеншн больше не трогаю».

## 5. Быстрый следующий эксперимент, если лог ничего не прояснит
Один запуск: **Indeed**, один ключевик `social media manager`, Miami, Hybrid=Any, Auto → должен дать
реальные подачи (Indeed 1-click). Это отделит «ZR-специфику» от «сломан движок».
