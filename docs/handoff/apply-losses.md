# apply-losses — где теряются подачи: хендбэки, потерянные записи, вход в Indeed

Обновлено: 2026-10-02 · ветка: main (#318 1.8.30 живая; #320 1.8.31 f7aadf3 живая в Chrome, пинг 1.8.31)

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

## Последний заход (10-02)

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

Модель: **Opus**. 1) 1.8.31 живая. 2) Прогон Indeed на 1.8.31 — после окна GH-замера jobflow-48 (до ~23:15Z 10-03); объявить соседям, «давай» Игоря уже было) → строки `🧾 sdr` и доля подач.
3) Вопрос Игорю про неотвеченные хендбэки (выше). 4) Починить `drive.py` для `/dashboard/campaign`.

Файлы лейна: `chrome-extension/content.js` (preferIndeedResume/structuredReviewSnapshot ~L3860, step loop ~L4325), `background.js` forgetHandedBackFromApplied ~L1059, `tests/indeed-resume-choice.test.js`, `tests/applied-rollback.test.js`, `chrome-extension/content.js` (formBlockers ~L3500, fillTextQuestions ~L2990,
pay/school helpers перед isDemographicQuestion, fillComboboxes, fillCheckboxes),
`background.js` ATS_JOB_FAILED, `tests/form-blockers.test.js`, `tests/filler-honest.test.js`.
