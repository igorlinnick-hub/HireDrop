# jobflow/ — FastAPI backend + Chrome MV3 расширение

Бэкенд и **движок подачи** HireDrop. Деплой: Railway (`Procfile` → `uvicorn app.main:app`).
Дашборд живёт отдельно в `../jobflow-website/`. Карта всех документов — [`../INDEX.md`](../INDEX.md).

**Статус платформ берётся ТОЛЬКО из `../STATUS_MATRIX.json`** (`../status.sh`). Любая статус-таблица
в прозе — включая `README.md` и `ROADMAP_E2E.md` — устаревает молча и уже врала.

## Что где

```
app/                FastAPI, всё под /api/v1
├── main.py         монтирует роутеры
├── deps.py         get_current_user — Supabase JWT
├── db/             по файлу на таблицу Supabase (jobs, applications, campaign,
│                   profile, usage, activity, subscriptions, tap_review, …)
└── routers/        по файлу на группу ресурсов
modules/            ИИ-слой + скрейперы
├── ai_cover_letter / ai_resume_tailor / ai_question_answer / ai_fit_judge / ai_job_scorer
├── filters.py salary_filter.py ats_checker.py ats_pdf_generator.py
└── platforms/      скрейперы (indeed, ats_boards, jobspy_platform, registry, …)
chrome-extension/   MV3 — background.js (SW, шлюз к API) + content.js (машина
                    состояний подачи) + ping.js (мост к дашборду) + popup
migrations/         SQL, применяется руками через Supabase SQL Editor
```

⚠️ `README.md` частично протух: описанных там `chrome-extension/anti_detect/` и
`modules/telegram_bot.py` **не существует**. Структуру сверяй с диском, не с README.

## Команды

```bash
pytest                 # с покрытием, порог --cov-fail-under=60 (задан в pyproject)
ruff check . && ruff format --check .
node --check chrome-extension/background.js   # ДО загрузки в Chrome (см. ниже)
../scripts/sync-ext.sh                        # выкатить расширение Игорю
```

## Грабли, которые уже стоили часов

**1. Расширение: репо — источник, Рабочий стол — копия.**
Игорь грузит unpacked из `~/Desktop/HireDrop-Ext` — это **ручная копия**, не симлинк.
Правка в `chrome-extension/` до него не доедет, пока не выполнен `../scripts/sync-ext.sh`.
Копия регулярно оказывалась старее репо → «я не вижу изменений».

**2. Активация правок в живом Chrome — разная по файлам.**
- `manifest.json` и `content.js` → нужен **полный OFF/ON** тумблер на `chrome://extensions`.
- `background.js` → достаточно `DEV_RELOAD`.
- После любого из них **перезагрузить вкладку дашборда** — `ping.js` умирает вместе со старым контекстом.
- `__hiredrop_loaded` — проба не из того мира, ей не верить.

**3. Синтаксическая ошибка в `background.js` убивает расширение молча.**
Service worker падает со статусом 15, и это выглядит как проблема авторизации. Всегда
`node --check` перед загрузкой — на этом однажды потеряли сессию, диагностируя «сломанный логин».

**4. Новая платформа = правка `manifest.json`.**
Без `content_scripts.matches` + `host_permissions` под её хост `content.js` **молча никогда
не инжектится**. Так Ashby был сломан в проде (#82). Проверка — лог `Content script alive on <host>`.

**5. Бэкенд ходит под `service_role` и обходит RLS.**
Значит **каждый** запрос обязан фильтровать по `user_id` — иначе IDOR. Это главный класс
уязвимости в проекте, кросс-тенантная утечка P0 уже случалась (закрыта 2026-06-29).

**6. Start/Stop кампании идёт НЕ через бэкенд.**
Дашборд достаёт расширение через `window.postMessage` → `ping.js`. Один только
`apiPost('/campaign/stop')` оставляет расширение работать.

**7. Auto/Tap живёт в трёх связанных местах** — `profile.submit_mode`, `chrome.storage.reviewMode`
в расширении, и панель/чип в campaign-view. Рассинхрон = Игорь видит панель Tap в режиме Auto.

**8. Видимое окно браузера обязательно** — требование политики Chrome Web Store.
Не предлагать headless/убрать превью; живое превью делается скриншотами через CDP.

## Стиль

- Отвечать Игорю по-русски, код и комментарии — по-английски.
- Коммитить только по просьбе. На `main` — сначала ветка.
- Миграции Supabase Игорь применяет руками; в PR это должно быть написано явным шагом.
