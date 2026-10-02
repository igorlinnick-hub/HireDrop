# filler-honest — ревью скептика A (10-02)

Порт pay/school ↔ night_shift/common.py: **0 расхождений на 733 строках** (все кейсы
tests/test_night_shift_rules.py + 60 краевых + 400 случайных). Сьюта 28/28.
Скептик B: отчёт не получен — перезапустить (320 GH-схем `form_coverage.py`, что терял
только текстовый шаг, падает ли новый тест на origin/main).

## Блокеры
1. **Демо-группа галочек глотает аттестацию внешней обёртки** (content.js ~2843-2860):
   `g.querySelector("legend")` берёт вложенную legend, `all` — все галочки под `g`. Внешний
   fieldset с вложенным Race + «I certify…» → certify не ставится (jsdom: `r2` только, n=1).
   Фикс: `:scope > legend` и только галочки с `closest("fieldset,[role='group']") === g`.
2. **Near-match школы берёт другой вуз** (`pickSchoolOption`, ветка `within(ow, words)`;
   в Python та же логика): «Columbia College Chicago» → «Columbia College» (Миссури);
   «Texas A&M University Corpus Christi» → «Texas A&M University». Фикс: exact → «Other»
   (и поправить night_shift тем же PR).

## Не блокеры
- Yes/no про зарплату («comfortable with the range?») теперь всегда хендбэк (как ночная смена).
- Ветка eligibility (~3396) отвечает «Yes» на «able to accept pay of $18/hr?» до pay-проверки.
- Ярлык ≤3 слов = pay ask: «Pay Frequency», «Paid time off?» → без ИИ/фолбэка.
- Сломанный react-select: 4 клика, 2 лога, до 4 вызовов ИИ (control + inner input), повтор
  снова зовёт chooseOption — кешировать выбор между попытками.
- Хендбэк может прийти на раунд раньше, если комбо принялся, но нечитаем (DIV вне сигнатуры).
- Текстовый шаг пропускает role=combobox кроме school/city — Indeed «Recent job title» как
  combobox не заполнится (разметки нет — проверить).
- «Which office location?» react-select всё ещё кликает opts[0] (старый баг).
- fillSchoolTypeahead ищет опции по всей странице — устаревшее меню может дать чужой «Other».

## Исправлено (10-01, следующая сессия)
- Блокер 1: группа = свои галочки (`closest(...) === g`) и своя `:scope > legend`. Тест с
  обёрткой `role=group` без legend: на старом коде `r2 n=1`, на новом `r2,ok n=2`.
- Блокер 2: near-match школы только «то же имя + пометка в скобках» и только единственный;
  подмножество слов больше не матчится — в content.js и `night_shift/common.py` (`_within` удалён).
  Тесты Columbia ×2 / Texas A&M в обеих сьютах.

## Скептик B (10-01)
Сьюта гардит изменение: на origin/main с подсаженными хелперами падают 12 проверок.
Ложной pay-классификации на 320 схемах нет. Комбобоксы, которые текстовый шаг больше не
трогает, — в основном yes/no, на которые он печатал прозу; единственная настоящая потеря —
штат.
- **Блокер (исправлен):** GH-селект «State of residence» (~21 обязательный в 320 схемах) ушёл из
  текстового шага в `chooseOption`, где правила штата не было: при исчерпанном бюджете ИИ —
  `options[0]` = Alabama. Теперь `stateListPick`: список из ≥10 штатов → штат профиля целиком
  (имя или код, «(US) X»), иначе null; без ИИ и фолбэка. Заодно ушёл старый баг подстроки «HI» → Michigan.
- **Исправлено:** pay-ветка текстового шага теперь ловит и `payQuestion` («expected hourly rate», «OTE»).
- Не блокеры, оставлены: свободный текст про зарплату получает фразу юзера и тогда, когда ярлык
  просит месяц/INR; yes/no «устраивает ли вилка» → хендбэк (по дизайну); 2 DOM-теста
  (inner `[role=option]`, повторное открытие react-select) проходят и на main — ужесточить;
  «Other» в `fillSchoolTypeahead` может не найтись при старом меню (уходит в хендбэк).
