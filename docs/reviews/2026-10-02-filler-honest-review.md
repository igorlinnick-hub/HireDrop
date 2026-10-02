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
