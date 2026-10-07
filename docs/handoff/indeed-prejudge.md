# indeed-prejudge — страница выдачи Indeed судится целиком ДО открытия вакансий

Обновлено: 2026-10-07 · ветка: `feat/indeed-prejudge` (worktree `~/Code/JobFlow/.wt-indeed-prejudge`), НЕ смержено, PR не открыт

## Зачем (замер 10-07, `run_history.py` + разбор лога)
- Indeed-прогон судил каждую карточку вживую: открыть /viewjob → судья → чаще всего отказ, **~18 с на отказ**, по одной.
  Прогон 07:47Z: 18 отказов подряд по «project manager», 0 подач. 05:17Z: 12 подач, 5.5 мин/заявку.
- Вердикт нигде не хранился (0 из 767 Indeed-строк) → **33% вызовов судьи с 09-22 — повторы** (INNIO ×6).
- «Замедление на 19-й» = Indeed кончился (доли ключевиков 2/фразу съел утренний прогон) → ушли на ZR (сломан, отдельный лейн).
- Игорь: ZR чиним отдельно, фразы не трогаем, сюда вкладываемся «чтобы работал как ракета».

## Что сделано (3 коммита: 3d0eb05, 1b1c352, b9ee647)
- **Сервер** `POST /tools/assess-fit-batch` (`app/routers/tools.py`): карточки страницы + полный текст → пишет текст в строку
  пула (insert-only), берёт текущий вердикт (`fit_version`), остальное судит параллельно (`fit_queue.judge_pending`,
  10 потоков, дедлайн 16 с), **сохраняет вердикт**; кап компании/Broad до ИИ; skipped/rejected/dead → skip без судьи;
  approved/queued → unjudged; текст <300 → unjudged; бюджет возвращается за несделанные вызовы; семафор 8/процесс.
  `/tools/assess-fit` с `job_id` отвечает из сохранённого вердикта и бюджет списывает только на вызов модели.
  `jobs_db.rows_by_links`, `save_jobs_bulk(insert_only=)` — `/jobs/ingest` теперь тоже insert-only. `run_report`:
  «Opened nothing: N postings judged … lost to fit gate» вместо «Nothing opened yet».
- **Расширение** (`content.js` блок «Search-page judge», `phase1_indeed`, `phase2_indeed`; `background.js` PREJUDGE_CARDS 28 с,
  ASSESS_FIT пробрасывает `job_id`): клик по карточке → текст из правой панели `.simple-job-description-html` (~0.45 с живьём)
  → пауза humanDelay(600,1500) → чанки по 5 уходят судье, пока читаются следующие → открываются только fit; отказы — строка
  `⏭️ Skipped (fit N)` (run_report считает) + `processedJobKeys`. Любой сбой → старый путь (судья на странице). Tap/pool не трогает.
- **`/rpc/jobdescs` ВЫКИНУТ**: отвечает (30 описаний/0.36 с), но страница Indeed сама его НЕ вызывает (проверено 10-07:
  панель грузит через apis.indeed.com/graphql) → сигнал бота. Фикстура панели: `tests/fixtures/indeed-serp-pane.html`.
- Тесты: `tests/test_assess_fit_batch.py` (17), `tests/test_run_report.py` (+1), `chrome-extension/tests/indeed-prejudge.test.js` (36).
  pytest зелёный (71%), ruff чистый; run-all: 49/50 — `review-wait-button.test.js` упал 1 раз в общем прогоне, отдельно проходит
  (похоже на тайминг; перепроверка ушла в фон и в новую сессию НЕ придёт — перезапустить).
- Скептики 1 и 2 (сервер, расширение): блокирующего нет, всё SHOULD-FIX применено в 1b1c352. **Скептик 3 на b9ee647 (клик по
  панели: повторный вход фазы через MutationObserver, `text !== before`, навигация clickEl, каденс кликов) был в работе при
  /clear — отчёт НЕ придёт, запустить заново.**

## Следующий шаг
Модель: **Opus**. 1) Скептик на b9ee647 заново → правки (кандидаты: ранний выход после N подряд нечитаемых панелей,
каденс кликов) → перепроверить run-all. 2) blast-radius (стыки: content/background, роутер tools, ingest) → PR → CI → мерж
(бэкенд уезжает на Railway сам; расширение — бамп версии после #384/#385, `sync-ext.sh`). 3) **Живой Indeed-прогон — ждёт
«давай» Игоря**: `drive.py run auto --platform indeed`, окна автоматизации закрыть по id; мерить `run_history.py` против
05:17Z (5.5 мин/заявку) и строки «⚡ Judged this page ahead in N s». 4) CWS-релиз. Пересекается с #384/#385 по content.js.
