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
migrations/         SQL; применяет сессия — `supabase db query --linked -f <файл>`
```

⚠️ `README.md` частично протух: описанных там `chrome-extension/anti_detect/` и
`modules/telegram_bot.py` **не существует**. Структуру сверяй с диском, не с README.

## Команды

```bash
pytest                 # с покрытием, порог --cov-fail-under=60 (задан в pyproject)
ruff check . && ruff format --check .
npm ci && npm run test:ext                    # JS-сьюта расширения (нужен раз: npm ci)
node --check chrome-extension/background.js   # ДО загрузки в Chrome (см. ниже)
../scripts/sync-ext.sh                        # выкатить расширение Игорю
```

**Замер прогона — `GET /tools/run-report`, НЕ сырой SQL.** Отдаёт воронку (открыто → подано),
минут на заявку, заявок в час и главную причину потерь одной строкой. Пока каждый меряет
по-своему, прогоны нельзя сравнить между собой: «сегодня быстрее» остаётся памятью, а не
замером. 21.09 целый день мерили SQL-запросами, хотя отчёт был написан с самого начала —
разброс темпа оказался десятикратным (1 заявка за 3 мин против 1 за 44), и увидеть это можно
было только сравнимыми числами. Соседние: `/activity/summary` (что-то сломано?),
`/activity/handbacks` (админский разбор незаполненных полей, только открытые и с лимитом —
агрегата за период он НЕ даёт).

**Сравнить прогоны между собой — `scripts/run_history.py`.** `.venv/bin/python
scripts/run_history.py --email <почта> [--days 14] [--gap-min 20]` режет лог на прогоны по
паузам и по каждому зовёт ТОТ ЖЕ `activity.run_report()` (с `until`), так что «до» и «после»
правки посчитаны одной формулой. 09-30, база до очереди с судьёй (Игорь, 11 прогонов
09-20…09-29): 643 мин, открыто 223 → подано 34 = **18.9 мин/заявку, 3.2/ч, 15% открытых**;
главные потери — fit gate и title mismatch. Отчёт не делит по платформам: Indeed и ATS вместе.

**Реклама (траты, подключения) — `scripts/ads_spend.py`, вкладка Ads в админ-борде.**
`.venv/bin/python scripts/ads_spend.py status` — что подключено (Meta spend / Meta CAPI /
Google ingest), строк и последний синк по платформам; `sync-meta --days N` — стянуть Meta
Insights сейчас (токен мёртв с 10-05); `ingest-mcp --date D <файл>` — один день ответа Meta Ads MCP
(`ads_get_ad_entities` level=ad) в `ad_spend`, так трату Meta пишет агент `ads-manager`; `add --platform manual --channel <utm_source> --date … --campaign … --spend …`
— трата в канале без API. Цифры CAC/ROAS/вердикты по объявлениям смотреть в секции `ads`
`GET /admin/metrics`, не считать руками. Хендофф: `docs/handoff/ads-board.md`.

**Как часто мы возвращаем форму человеку — `scripts/measure_handback_share.py`, и мерить
ДОЛЮ, а не счёт.** `.venv/bin/python scripts/measure_handback_share.py [--days N]` печатает
handback/apply по платформам, общую долю и знаменатель рядом с ней. Зачем именно доля: 09-26
абсолют читался как «39 хендбэков за 30 дней» — звучит как угол и провоцирует «отложим», — а
в том же окне было 95 заявок, то есть **29% завершённых попыток**, на Greenhouse 12 против 11
и на Ashby 4 против 2: на ATS-бордах хендбэк исход ЧАЩЕ заявки. Гонять ДВА окна: `--days 7`
там же дало 46% (36 из 39 хендбэков — за последнюю неделю), 30-дневное число усредняет
простой с загрузкой и занижает то, что продукт делает сейчас.

**ПОЧЕМУ возвращаем форму — `scripts/handback_reasons.py [--days 14] [--platform P] [--email E]`**: платформа × категория причины (email-код, Indeed resume review, Continue refused, пустые обязательные…), last seen, топ `unfilled`, повторы и сколько отданных заявок всё же ушли; фикс филлера выбирать по нему, не по глазу (10-06: 67 событий/14д — GH «пустые поля» 20 из 27 ложные до #307, Indeed resume review 15 — главный живой).

**Знает ли пул, сколько платят — `scripts/measure_salary_fill.py`.**
`.venv/bin/python scripts/measure_salary_fill.py [--user <uuid>]` печатает три числа, которые
решают любой спор про зарплатный фильтр: сколько аккаунтов вообще задали вилку (09-26: **2 из
40**), доля строк с ЧИТАЕМОЙ платой по платформам (**13.3%**: Indeed 44%, Greenhouse 9%,
Ashby/Workday 0% — колонки salary в `jobs` нет, плата парсится из текста) и цена каждого
варианта гейта на живых данных. Зачем гонять ПЕРЕД правкой фильтра: «unknown режектить»
оставляет **6.7% деки** и заблокировал бы 123 из 124 реальных заявок, а «unknown пропускать» —
93%. Тот же прогон ловит регрессии парсера: он же показал, что чип Indeed
(`$210,000 - $250,000 a yearFull-time`) читался у нас как «плата не указана» в 319 из 900 строк.

**Прав ли судья и сколько вакансий реально подходит — `scripts/measure_judge_calibration.py`.**
`.venv/bin/python scripts/measure_judge_calibration.py --user <uuid> [--user …] --per-user 200
--budget 3.0 --out <dir>` гоняет боевой `assess_fit` по свежей колоде юзера: полосы оценок,
сколько проходит его планку, сколько срежет кап компании, «N подходят в день», цена, и по 20
отказов на профиль — ЧИТАТЬ ГЛАЗАМИ. `--dry-run` — только воронка ($0), `--report-only` —
пересчёт по JSONL. Гонять до/после любой правки промпта судьи или каскада. 09-30: Игорь 0/20
ошибочных отказов, Антония 6/20; 47–99% строк без описания; $0.0067 за вакансию, а не $0.003.

**Можно ли включать CSP сайта — `scripts/csp_violations.py`, НЕ `railway logs | grep`.**
`.venv/bin/python scripts/csp_violations.py [--days 7]` сводит строки `[csp]` (приёмник
`app/routers/csp.py`) по ВСЕМ деплоям за окно; exit 0 = чисто, можно переносить
Report-Only в `Content-Security-Policy`. Простой grep читает только последний деплой: 10-05 он
сказал «чисто», а единственное настоящее нарушение лежало в предыдущем.

**Письмо «удалите мой аккаунт» — `scripts/delete_account.py`.** Это обещание политики
приватности (jobflow-website `app/privacy/page.tsx`): стереть профиль, резюме, историю, сессии
за 30 дней. `.venv/bin/python scripts/delete_account.py --email <почта>` (или `--user-id`) —
**по умолчанию dry-run**: строки по таблицам, файлы `resumes/<uid>/`, auth-юзер, ничего не
удаляет. `--execute --confirm <user id из dry-run>` (id, не почта: опечатка ×2 в почте стёрла
бы другого) стирает: бан auth-юзера → файлы → строки (пересчёт до нуля) → `auth.admin.delete_user`
→ досбор того, что успел дописать уже выданный токен; печатает квитанцию для лога ответа. **Отказывает (exit 2,
ничего не тронуто)**: живая Stripe-подписка (ищется по id из профиля И по почте — checkout без
customer пишет только `customer_email`) или Stripe не ответил — сначала отменить; аффилиат
с commissions/payouts — деньги храним по закону, руками; юзер пришёл по рефке и аффилиат
заработал на нём, пока не применена `migrations/referrals_survive_account_erasure.sql` (иначе
каскад снёс бы ЧУЖОЙ леджер; с ней реферал анонимизируется). Exit 3 = в живой схеме колонка
`*user_id`/`*email`/`*affiliate_id`/`*_customer_id`/…, которой нет в `RULES` скрипта — новую таблицу
классифицировать; или в схеме НЕТ таблицы, нужной плану (пустой/чужой ответ OpenAPI раньше молча
пропускал все проверки, включая денежную).

**JS-тесты расширения в CI с 09-21.** `chrome-extension/tests/*.test.js` гоняются на каждом
PR (`npm run test:ext` → `tests/run-all.js`, находит файлы сам — регистрировать новый не
нужно). До этого сьюта жила ВНЕ CI: workflow гонял только Python, поэтому три рабочих теста
никто не проверял, а два (`consent-gate`, `detection-visibility`) месяцами падали из-за
отсутствующего `jsdom`. `package.json` в корне `jobflow/` — **dev-only и private**: ничего
не публикуется, расширение npm-кода не грузит, единственная зависимость нужна только тестам.

## Параллельные сессии: имя, лейн, цель — `docs/sessions/`

SessionStart-хук (`.claude/settings.json` → `scripts/sessions.py hook`) сам даёт сессии имя
(`amber-otter`, из id сессии) и печатает доску: кто жив, какой лейн, цель, чьи файлы пересекаются.
Как только задача понятна — `python3 scripts/sessions.py claim --session <id> --lane <лейн> --goal
"<что значит готово>" --now "<шаг>" --scope <файлы>`; шаг сменился — `beat --now`; лейн закрыт —
`done`. ⚠ ПЕРЕСЕЧЕНИЕ на доске = не правь эти файлы, пока не договорился через хендофф той сессии.
Хендофф подписывать первой строкой `Сессия: <имя> · лейн <лейн>`. Подробности — `docs/sessions/README.md`.

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
- **Миграции Supabase применяет сессия сама** (09-05): `supabase link --project-ref
  msxjcjzmfruizbgkssxo --yes` во временной папке → `supabase db query --linked -f
  migrations/<файл>.sql` → `notify pgrst, 'reload schema'` → проверить колонку через REST.
  Игоря просить не нужно. **Порядок обязателен**: миграция ПЕРЕД мержем website-PR —
  карточка Settings пишет напрямую в PostgREST, и неизвестная колонка (PGRST204) валит
  весь сейв профиля, а не только новое поле.
