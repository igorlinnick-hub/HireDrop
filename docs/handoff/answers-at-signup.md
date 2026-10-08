# answers-at-signup — вопросы работодателей + одно окно проверки перед первым прогоном

Сессия: azure-heron · лейн answers-at-signup · цель: окно «проверь, что уйдёт работодателям» один раз перед первым прогоном — в проде и проверено живьём · шаг: blast-radius перед мержем · доска: `python3 scripts/sessions.py board`

Обновлено: 2026-10-08 · в проде: HireDrop #380, web #294, web #299 · ОТКРЫТЫ: HireDrop #397 + web #314 (окно проверки)

## Состояние

Вопросы работодателей — один список на сервере: `modules/employer_answers.py` (11 вопросов, `stage`
signup/resume, `OPT_OUT`, `SINCE`, `ANSWERS_UI`). Start-гейт требует все 11; меняется только КОГДА спрашиваем.

Что видит человек (в проде):
- шаг 1 онбординга — «Do you live in the United States?» (web #299): No → «Sorry — HireDrop works only in
  the United States», Continue закрыт; Yes пишется на сервер; на шаге 7 вопрос не повторяется (web #294).
- шаг 7 — остальные вопросы: с резюме все, подсказки из ОРИГИНАЛЬНОГО PDF (`profiles.resume_facts`) группой
  «From your resume — check these» + «Looks right»; без резюме — только signup-stage.
- Start-модалка спрашивает то, чего не хватает (старые аккаунты).

Окно проверки перед первым прогоном (HireDrop #397 + web #314 — ОТКРЫТЫ, проверено вживую локально):
- `modules/review_sheet.py` — строки окна: контакты, 11 вопросов, ZIP, «2 weeks»/«Fluent» (то, что филлер
  шлёт молча), строка EEO; `GET/POST /profile/review`; `profiles.answers_confirmed_at` — миграция
  `add_answers_confirmed_at.sql` **применена в прод 10-08**.
- Положено только аккаунту, который ни разу не запускал (нет `campaign_states.started_at` и ни одной заявки)
  и не подтверждал — `campaign_db.review_due`; ошибка чтения → не спрашиваем (пишем в stderr).
- Start отказывает `review_missing` последним; readiness строка `review` (fix `review`) — только для
  `answers_ui >= 3` (сервер ANSWERS_UI=3, сайт шлёт 3 после web #314). Tap держит тоже.
- Сайт: `components/dashboard/ReviewSheet.tsx` в Start-модалке, ПОСЛЕ формы недостающих ответов.
- Охват (прод 10-08): 47 профилей → 7 уже запускали (не спросим), 5 онбордились и не запускали (спросим раз),
  35 не закончили онбординг.

## Последний заход (10-07…10-08)

- Игорь: юзеру 99d0d45b (сварщик из Канады) и работодателям НЕ пишем; ответ на такие случаи — US-only гейт.
- web #299 и web #294 смержены; #294 проверен живьём под `+buyer1` с выдуманным резюме (Jordan Avery, Austin):
  подсказки верные, сохранение в БД верное, обе темы, 390–1920.
- Замер хендбэков за 30 дней: 41 → 33 на аккаунте Игоря (наши тесты), 8 у двух настоящих юзеров; ответов 0.
- Окно проверки: бэкенд + сайт, тесты (+18 py, +7 ts), стандарт кода (#393/web #312) соблюдён. Живая проверка:
  локальный бэкенд ветки + локальный сайт + прод-БД, `+buyer1` → окно → «Everything's correct» →
  `answers_confirmed_at` записан, Start ready. Blast-radius: 2 агента-опровергателя — вердикт в PR #397.
- `+buyer1` теперь: резюме Jordan Avery, 11 ответов, `answers_confirmed_at` стоит (для новой живой проверки — обнулить).

## Сломано / не доделано

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

Модель: **Opus**. 1) Вердикт двух агентов blast-radius → починить найденное → мерж web #314, потом HireDrop #397
(старый бэкенд `answers_ui=3` игнорирует — этот порядок безопасен) → после деплоя Railway обнулить
`answers_confirmed_at` у `+buyer1`, пройти окно на hiredrop.io живьём → `sessions.py done`.
2) Передать ext-лейну задание из «Сломано».

Файлы: бэкенд — `modules/{employer_answers,review_sheet,ai_resume_facts}.py`, `app/routers/{profile,campaign}.py`,
`app/db/{profile,campaign}.py`, `migrations/add_answers_confirmed_at.sql`; сайт —
`components/dashboard/{ReviewSheet,EmployerAnswersForm,StartReadiness}.tsx`,
`components/onboarding/{StepPersonalInfo,StepEmployerAnswers}.tsx`, `lib/{reviewSheet,usResident,employerAnswers,employerAnswersGate}.ts`.
