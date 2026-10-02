# apply-losses — где теряются подачи: хендбэки, потерянные записи, вход в Indeed

Обновлено: 2026-10-02 · ветка: main (смержены #305, #318 filler-honest; ext 1.8.30 живая у Игоря)

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

- **Прогон Indeed на 1.8.30** (21:21–21:46Z, `/tools/run-report`): 26 мин, открыто 4, подано 1
  (без подтверждения), хендбэк 3; потери: fit gate 12, title mismatch 7, dead links 3.
- **Все 3 хендбэка — `structured-data-review`**, тот же след, что 14 из 17 старых: «Review details» →
  Continue ×2 → отказ. Строка 🖐 (#305) пустая: `alerts=[] invalid=[] reqEmpty=[]`,
  notes=«Review your resume details», кнопка Continue есть. Т.е. отказ без видимой ошибки — диагностика
  #305 здесь ничего не ловит. (`metadata_json.diag` = null — для Indeed хендбэки в таблицу `handbacks`
  не пишутся, только `activity_log`.)
- **Зацепка (гипотеза, не доказана):** на `resume-selection` бот выбирает загруженный `resume.pdf`, а
  не «Use your Indeed Resume» (Indeed помечает его Recommended). Шаг structured-data-review Indeed
  вставляет именно для разобранного файла. Проверка: выбрать Indeed Resume и посмотреть, исчезает ли
  шаг — нужен живой клик Continue в настоящей заявке (классификатор auto-mode запрещает) → Игорь
  руками один раз, или прогон с правкой выбора резюме.
- `drive.py` сошёл на 98-й секунде: «page has no data-testid» — после Start дашборд уходит на
  `/dashboard/campaign`, а драйвер, похоже, смотрит не ту вкладку/путь. Кампания шла дальше; Stop
  нажат таймером по `data-testid=btn-stop` в 21:46Z.

## Сломано / не доделано

- ✅ GH-хендбэки переочередены правилом продукта: #319 (jobflow-2f) — новой сборке ext одна
  попытка на открытый хендбэк. 9 хендбэков Игоря отпущены под 1.8.30.
- Не блокеры из ревью (в файле ревью): 2 DOM-теста filler-honest проходят и на старом коде —
  ужесточить; свободный текст зарплаты не пересчитывает месяц/INR.
- structured-data-review: причина не снята. Нужен Игорь (дойти до «Review details» руками)
  или первый хендбэк после релиза — `metadata.diag` скажет.
- #280 (продление сессии Indeed) живьём не доказан. Кап по локальному дню — не начат.

## Следующий шаг

Модель: **Opus**. Решить structured-data-review: (1) Игорь один раз руками на Indeed выбирает «Use
your Indeed Resume» и смотрит, есть ли шаг «Review details»; или (2) снять разметку шага
`structured-data-review` (Игорь доходит до него, сессия читает DOM без кликов) — что там требует
действия без aria-invalid. Затем фикс выбора резюме / шага. Отдельно: починить `drive.py` для
`/dashboard/campaign`.

Файлы лейна: `chrome-extension/content.js` (formBlockers ~L3500, fillTextQuestions ~L2990,
pay/school helpers перед isDemographicQuestion, fillComboboxes, fillCheckboxes),
`background.js` ATS_JOB_FAILED, `tests/form-blockers.test.js`, `tests/filler-honest.test.js`.
