# answers-at-signup — вопросы работодателей + одно окно проверки перед первым прогоном

Сессия: vivid-lemur · лейн answers-at-signup · цель: окно «проверь, что уйдёт работодателям» один раз перед первым прогоном — в проде и проверено живьём · шаг: ЗАКРЫТО · доска: `python3 scripts/sessions.py board`

Обновлено: 2026-10-09 · в проде: HireDrop #380, #397, web #294, #299, #314 · лейн закрыт (окно в проде, проверено живьём)

## Состояние

Вопросы работодателей — один список на сервере: `modules/employer_answers.py` (11 вопросов, `stage`
signup/resume, `OPT_OUT`, `SINCE`, `ANSWERS_UI`). Start-гейт требует все 11; меняется только КОГДА спрашиваем.

Что видит человек (в проде):
- шаг 1 онбординга — «Do you live in the United States?» (web #299): No → «Sorry — HireDrop works only in
  the United States», Continue закрыт; Yes пишется на сервер; на шаге 7 вопрос не повторяется (web #294).
- шаг 7 — остальные вопросы: с резюме все, подсказки из ОРИГИНАЛЬНОГО PDF (`profiles.resume_facts`) группой
  «From your resume — check these» + «Looks right»; без резюме — только signup-stage.
- Start-модалка спрашивает то, чего не хватает (старые аккаунты).

Окно проверки перед первым прогоном (HireDrop #397 + web #314 — в проде с 10-09, проверено живьём на hiredrop.io):
- `modules/review_sheet.py` — строки окна: контакты, 11 вопросов, ZIP, «2 weeks»/«Fluent» (то, что филлер
  шлёт молча), строка EEO; `GET/POST /profile/review`; `profiles.answers_confirmed_at` — миграция
  `add_answers_confirmed_at.sql` **применена в прод 10-08**.
- Положено только аккаунту, который ни разу не запускал и не подтверждал. «Запускал» = `started_at`, ИЛИ
  ключ `filters.kw_cursor` (пишет каждый Start, `stop()` сохраняет — `started_at` стоп обнуляет), ИЛИ заявка — `campaign_db.review_due`; ошибка чтения → не спрашиваем (пишем в stderr).
- Start отказывает `review_missing` последним — ТОЛЬКО если клиент ЯВНО прислал `answers_ui >= 3`
  (`review_asked`). Расширение answers_ui не шлёт → `/campaign/status`/`start` из попапа/auto-daily окно не
  требуют (иначе «Couldn't reach HireDrop»). readiness строка `review` (fix `review`) — только для явного `answers_ui >= 3` (сервер ANSWERS_UI=3, сайт шлёт 3 после web #314). Tap держит тоже.
- Сайт: `components/dashboard/ReviewSheet.tsx` в Start-модалке, ПОСЛЕ формы недостающих ответов. Отказ
  сервера по непустой строке (противоречащие ответы) показывает `note` сервера и обводит строку (`serverNotes`).
- Охват (прод 10-08): 47 профилей → 7 уже запускали (не спросим), 5 онбордились и не запускали (спросим раз),
  35 не закончили онбординг.

## Последний заход (10-09, vivid-lemur)

- 3 фикса по вердикту агентов (п.1, п.5, п.6 прошлого «Сломано») → web #314 смержен первым, HireDrop #397
  перебазирован на main (конфликт `app/db/profile.py` с `personal_facts` — оставлены оба поля), смержен.
  Тесты: pytest 1701 ✓, web 108 ✓.
- Прод (Railway 10:46 HST), под `+buyer1` (обнулил `answers_confirmed_at`): readiness `answers_ui=3` → review ✗;
  `answers_ui=2` и без параметра (расширение) → не требует; `/campaign/status` → `start_refusal: null`.
  hiredrop.io (headless Chromium) → Start Campaign → окно со всеми секциями → «Everything's correct» → окно
  ушло, осталась только строка «Install the extension»; `answers_confirmed_at` записан в прод-БД.
- Грабли: `profiles` ключ `user_id`, не `id` — мой первый SQL-джойн по `id` показал «не подтверждено» ложно.

## Сломано / не доделано

- Мелочи окна (не блокеры): QuickActions покажет сырой `review_missing`, если гейт не ответил за 6 с (`:571`);
  строка вопроса в ReviewSheet дублирует рендер EmployerAnswersForm — вынести общий компонент.
- 🔴 **Задание для ext-лейна (`content.js` сейчас держит maple-stoat) — не взято:**
  1. Повторная подача Indeed/ZR после ответа на хендбэк. Очереди у них нет (`_QUEUE_PLATFORMS` в
     `app/db/handbacks.py` — только GH/Lever/Ashby; `requeueable()` = job_id или ATS): ответ сохраняется,
     заявка не уходит. Нужно: в начале Indeed/ZR-обхода открыть URL хендбэков с `requeued_at`
     (`GET /handbacks`; `background.js` ~1186 уже снимает им отметку «подано») и пройти форму; на бэкенде
     `requeueable()` → true для indeed/ziprecruiter, когда расширение это умеет.
  2. Только ПОСЛЕ п.1 — убрать угадывание: радио «Yes / первый вариант» на незнакомый вопрос (`content.js`
     ~3526), дропдаун «первый вариант» (~4633), школа/степень в дропдауне → хендбэк вместо выдумки.
  3. «2 weeks»/«Fluent» по умолчанию (`content.js` ~3944/3946): у прошедших окно значения явные; у старых
     пустых — хендбэк вместо дефолта.
  4. Ответ хендбэка → следующие формы: точное совпадение вопроса и вариантов, не auth/sponsorship, не EEO
     (бэкенд: при `POST /handbacks/{id}/answers` класть закрытые ответы в `screener_answer_cache`).
  5. Сторовый ext показывает отказ Start как «Couldn't reach HireDrop…» — показывать `start_refusal`
     (теперь и `review_missing`).
- Дашборд на телефоне (390): Find Jobs / Start Campaign вылезают за экран, страница 630px (QuickActions).
- Поле Email на шаге 1 онбординга никуда не пишется (формы берут email аккаунта) — вводит в заблуждение.

## Следующий шаг

Лейн закрыт. Открыто только задание ext-лейну (выше, п.1–5) — передать сессии, которая держит `content.js`.
Модель для ext-задания — **Opus**.
