# apply-losses — где теряются подачи: хендбэки, потерянные записи, вход в Indeed

Обновлено: 2026-10-05 · ветка: main (#333 1.8.32 84ae853 смержен, синкнут на Рабочий стол; в Chrome подхватится релоадом драйвера перед прогоном)

## Состояние

- **Хендбэк Indeed за 30 дней: 29, все «Continue refused, nothing left to fill».** 12 несут
  метки (пустой профиль: recent title/employer 5+5, адрес 3 — не код). **17 без единой
  зацепки**: 14 на `smartapply…/resume-module/structured-data-review` (след у 3 юзеров
  одинаковый: «Review details» → Continue ×2, filled=[]), 3 на `demographic-questions`
  (filled=[radio,combo×1] → filled=[] → хендбэк). Скрипты разбора (только чтение
  `activity_log`): шаблон в истории сессии 10-02; инструмента в репо нет.
- **#305 (смержен, 7c78887) — возврат теперь пишет, на что ругается форма**: `formBlockers()`
  рядом с `dialogSnapshot()` в content.js → строка 🖐 + `metadata.diag` хендбэка (НЕ в
  `/handbacks.questions`) + `@<path>` в каждой STEP-строке. Тексты ошибок, aria-invalid,
  пустые обязательные ARIA-виджеты, путь, disabled кнопки, при пустоте — предупреждения/
  заголовки страницы. Значения полей не читаются; почта/телефоны маскируются; строки ≤1950.
  В Chrome Игоря с 1.8.30 (замер `GET /extension/ping` → 1.8.30, 10-02).
- **Рой bug-hunt (9 агентов)**: по structured-data-review/демографии ничего не доказал без
  живой страницы; нашёл мёртвую проверку в `fillComboboxes` (hdDone до «принялось ли»).
- **#318 filler-honest СМЕРЖЕН (debcd99), ext 1.8.30 синкнута + DEV_RELOAD, пинг = 1.8.30.**
  Зарплата только из `profile.salary_expectation` (порт ночной смены, 0 расхождений на 733
  строках); school/degree из профиля, школа = точное или «то же имя + скобки», иначе «Other»
  (подмножество слов — другой вуз; то же в `night_shift/common.py`); штат проживания
  (`stateListPick`) = штат профиля целиком по имени/коду, иначе null — без ИИ и фолбэка;
  выбор в дропдауне считается, только если принялся; текстовый шаг не трогает комбобоксы
  кроме school/city; демо-группа галочек = свои галочки и своя legend → «decline».
  2 скептика: [ревью](../reviews/2026-10-02-filler-honest-review.md) — блокеры A×2 и B×1 закрыты.
- Раздел content.js с jobflow-9f: его #307 (смержен, 7e69156) — `collectUnfilledRequired` +
  гейт email-кода GH. Остальной заполнитель — этот лейн. jobflow-8f — сервер/очередь (#306:
  очередь пропускает открытые хендбэки).

## Последний заход (10-05)

- **Прогон Indeed на 1.8.31** (21:37–22:03Z, `run_history.py --since/--until`): открыто 7, подано 2,
  **12.5 мин/заявку, 4.8/ч** (база 18.9). Потери до формы: fit gate 19, title mismatch 9.
- **#320 работает**: 1 отказ structured-data-review на загруженном PDF (21:44, Bowtech) → дальше
  везде `filled=[indeed-resume]`, SDR больше не встретился. Строка `🧾 sdr resume=file badges=[]`
  — бейджей нет, действия только Add/Edit/Remove: чего ждёт шаг, всё ещё не видно.
- **Главная потеря Indeed теперь — последняя страница**: `review-module` → `btn="-" (none)` →
  «Form abandoned without submit». 10-05: 10 из 12 дошедших до review; с 10-02 — 15 из 18
  (подсчёт по STEP-строкам с `@…review-module`). Все шаги до неё заполнены.
- **#333 (смержен, 84ae853) ext 1.8.32**: `isShownControl()` вместо `offsetParent !== null` в
  `findFormButtonIn`/`isSubmitStep` — fixed-кнопка с меткой submit/continue/review/next и
  реальной рамкой засчитывается (offsetParent = null у самого fixed-элемента). Закреплённый
  «Apply for this job» по-прежнему не берётся. **Это гипотеза** — живую страницу снять не дали
  (клик Apply на аккаунте Игоря заблокирован классификатором). Поэтому же на выходе no-button
  пишется `🔘 @path iframes=N shadows=N btns=[testid:label[op,fx:fixed,box,off]…]` — следующий
  прогон скажет, fixed это, iframe, shadow DOM или кнопки нет вовсе.
  Тест `tests/review-submit-visible.test.js` (7 проверок падают на старом коде), сьюта 31/31.
- `drive.py` (корень, a28021a): Playwright-Chrome соседа (`channel="chrome"`) перехватывает
  AppleScript-цель «Google Chrome» → драйвер падал на 89-й секунде без Stop. Теперь ждёт окно 120 с
  и отказывается стартовать, пока такой Chrome запущен.
- В окне 639094017 осталась вкладка viewjob jk=55486472022d511e (закрыть не дал классификатор).

## Заход 10-02

- **Прогон Indeed на 1.8.30** (21:21–21:46Z, `/tools/run-report`): открыто 4, подано 1, хендбэк 3.
  **Все 3 = загружен PDF (`filled=[resume]`) → structured-data-intro → structured-data-review →
  Continue ×3 → отказ**, без alert/aria-invalid/пустых полей. Единственная подача — без загрузки
  (сразу review-module). Т.е. не «через раз», а каждый раз, когда грузим PDF.
- **#320 смержен (f7aadf3), ext 1.8.31**, 3 скептика (все блокеры закрыты):
  - `preferIndeedResume()` — после первого отказа на structured-data-review (`indeedSdrRefusedAt`,
    14 дней, user-scoped) на resume-selection берём «Use your Indeed Resume» вместо загрузки;
    прогресс только при смене выбора (иначе stall guard не срабатывал → 20 кругов без хендбэка).
  - `structuredReviewSnapshot()` — на отказе строка `🧾 sdr resume=<file|indeed> badges=… acts=… ids=…`
    (только листовые бейджи и 2 слова действий — без текста резюме). Следующий прогон скажет, чего
    ждёт шаг → потом вернуть tailored PDF на Indeed.
  - `forgetHandedBackFromApplied()` (background) — находка jobflow-2f: «applied» ставится ДО Submit,
    хендбэк после клика его не снимал → buildAtsQueue выкидывал вакансию навсегда (7 из 9 GH).
    Теперь перед фильтром очереди снимаем отметки ТОЛЬКО у ОТВЕЧЕННЫХ хендбэков (`requeued_at`).
    Неотвеченные — нет: человек мог дослать руками («введи код и отправь»), снятие = двойная подача.
  - Тесты: `indeed-resume-choice.test.js` (фикстура снята с живой страницы
    `tests/fixtures/indeed-resume-selection.html`), `applied-rollback.test.js`; сьюта 30/30.
- `drive.py` сходит на 98-й секунде: «page has no data-testid» — после Start дашборд на
  `/dashboard/campaign`. Кампания шла; Stop нажат по `data-testid=btn-stop`.

## Сломано / не доделано

- 1.8.31 доехал (пинг 1.8.31) после повторного синка: `~/Desktop/HireDrop-Ext` был dataless (iCloud
  выгрузил Рабочий стол) — перед релоадом `stat -f "%b %Sf"`; может выгрузиться снова. Не делать
  `sync-ext.sh | head` — SIGPIPE рвёт синк.
- ✅ Неотвеченные хендбэки — решение Игоря 10-02: повтор только кнопкой. jobflow-2f откатил #319
  (#322, 603a43f); «Try again» в History (web #243) → `POST /handbacks/{id}/retry` → `requeued_at`
  → наш #320 снимает локальную отметку «подано». Повторный хендбэк сбрасывает `requeued_at`.
  Поле `newer_build` в `/handbacks` — можно показать кнопку и в попапе расширения (не сделано).
- Indeed Resume у Игоря «Created more than a week ago» — после переключения работодатель получит
  его, а не tailored PDF; ответы скринера всё ещё из резюме HireDrop.
- Не блокеры filler-honest — в файле ревью.

## Следующий шаг

Модель: **Opus**. 1) По «давай» Игоря: прогон Indeed на 1.8.32 (`drive.py run auto --minutes 25`)
→ доля review-module SUBMIT vs no button; на каждый no-button читать строку `🔘` — если там
`fx:fixed` без `op`-проблем, гипотеза не та; если `iframes>0`/`shadows>0` — Submit вне досягаемости
querySelectorAll. 2) Следом GH-замер (передан от jobflow-12/daily-30): `drive.py run auto --minutes 25
--platform greenhouse`, мерить строго окном прогона; база GH 21.0 мин/попытку (n=7).
3) Вернуть tailored PDF на Indeed — когда `🧾 sdr` покажет, чего ждёт шаг.

Файлы лейна: `chrome-extension/content.js` (isShownControl/buttonCensus после findFormButtonIn ~L3970, preferIndeedResume/structuredReviewSnapshot ~L3860, step loop ~L4325), `background.js` forgetHandedBackFromApplied ~L1059, `tests/indeed-resume-choice.test.js`, `tests/applied-rollback.test.js`, `chrome-extension/content.js` (formBlockers ~L3500, fillTextQuestions ~L2990,
pay/school helpers перед isDemographicQuestion, fillComboboxes, fillCheckboxes),
`background.js` ATS_JOB_FAILED, `tests/form-blockers.test.js`, `tests/filler-honest.test.js`.
