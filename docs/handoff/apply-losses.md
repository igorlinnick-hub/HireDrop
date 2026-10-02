# apply-losses — где теряются подачи: хендбэки, потерянные записи, вход в Indeed

Обновлено: 2026-10-02 · ветка: main (смержены #305; в работе ветка `ext/filler-honest`)

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
  **В Chrome Игоря его ещё НЕТ** (на Рабочем столе 1.8.23; main уже 1.8.28+) — нужен релиз.
- **Рой bug-hunt (9 агентов)**: по structured-data-review/демографии ничего не доказал без
  живой страницы; нашёл мёртвую проверку в `fillComboboxes` (hdDone до «принялось ли»).
- **Ветка `ext/filler-honest` (a1287ea, запушена, НЕ смержена, PR не открыт)** — content.js +
  `tests/filler-honest.test.js`, сьюта 28/28:
  зарплата из `profile.salary_expectation` (порт `salary_answer`/`pay_question` ночной смены,
  только вилка, которая держит сумму; pay-вопрос в `chooseOption` не идёт к ИИ и не берёт
  «первый вариант»); school/degree из профиля + строгий `fillSchoolTypeahead` (точное/
  уникальное/«Other»/ничего); текстовый шаг пропускает комбобоксы кроме school/city;
  `fillComboboxes` считает выбор, только если принялся (1 повтор → hdSkip + лог), клик по
  внутреннему `[role=option]`, `comboValue` читает `reactSelectShownValue`; демографическая
  группа галочек → «decline», без него — не трогать.
- Раздел content.js с jobflow-9f: его #307 (смержен, 7e69156) — `collectUnfilledRequired` +
  гейт email-кода GH. Остальной заполнитель — этот лейн. jobflow-8f — сервер/очередь (#306:
  очередь пропускает открытые хендбэки).

## Последний заход (10-01…10-02)

- Синкнул 1.8.23 (#280) на Рабочий стол; прогоны вела jobflow-8f (GH 25 мин: 2 подано).
- Живой съём structured-data-review: дошёл до questions/1 заявки SKIN Kahala
  (jk=63efdacc3718814e) — вкладка оставлена в окне дашборда 639093092. **Классификатор
  auto-mode запретил жать Continue в настоящей заявке** («Real-World Transactions»). Чтение
  страницы разрешено. Увидел: бот выбирает резюме-файл, не «Indeed Resume»; Indeed помнит
  ответы прошлой попытки.

## Сломано / не доделано

- `ext/filler-honest`: 2 скептика запущены, их отчёты НЕ получены (сессия закрыта) →
  перезапустить ревью (промпты: порт-верность против `tests/test_night_shift_rules.py`,
  320 GH-схем `form_coverage.py`, что из комбобоксов терял только текстовый шаг).
- Релиз расширения (бамп → `../scripts/sync-ext.sh` → DEV_RELOAD/OFF-ON → сверка версии)
  не сделан — включит #305, #307 и filler-honest.
- 8 запаркованных GH-хендбэков Игоря (DoorDash ×2, Twilio ×2, Later ×2, Muck Rack, Amwell)
  переочередить после мержа filler-honest (очередь их пропускает с #306).
- structured-data-review: причина не снята. Нужен Игорь (дойти до «Review details» руками)
  или первый хендбэк после релиза — `metadata.diag` скажет.
- #280 (продление сессии Indeed) живьём не доказан. Кап по локальному дню — не начат.

## Следующий шаг

Модель: **Opus**. Перезапустить 2 скептиков на `git diff origin/main...ext/filler-honest` →
починить блокеры → PR + мерж → релиз ext (бамп + sync + reload) → переочередить 8 GH →
прогон auto по Indeed (Игорь залогинен, крышка открыта, «давай») → прочитать
`metadata.diag` новых хендбэков (`activity_log`, type=handback) против 17 «без зацепки».

Файлы лейна: `chrome-extension/content.js` (formBlockers ~L3500, fillTextQuestions ~L2990,
pay/school helpers перед isDemographicQuestion, fillComboboxes, fillCheckboxes),
`background.js` ATS_JOB_FAILED, `tests/form-blockers.test.js`, `tests/filler-honest.test.js`.
