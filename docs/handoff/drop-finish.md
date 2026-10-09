Сессия: calm-quail · лейн drop-finish · цель: Хендбэк: «Let Drop finish it» перезаполняет форму в видимом окне до стены; логистика → Yes; копия Q&A в History · шаг: п.1+п.2 выпущены в 1.8.46, п.3 спроектирован · доска: `python3 scripts/sessions.py board`

# drop-finish — хендбэк доводится роботом, человек делает один шаг

Обновлено: 2026-10-09 · ветка: main

## Состояние

- **П.1 логистика → «Yes»: выпущен в ext 1.8.46 (#410, #414).** Бэкенд `_willing_yes`
  (`modules/ai_question_answer.py`) работает до модели, но после личного ответа человека
  (personal_facts, #401). Расширение: `isWillingnessQuestion` + `fillCheckboxes` + Ashby yes/no-кнопки.
  Тест на живой разметке Suno.
- **П.2 копия Q&A в History: в main (#411 + web #327), колонка `applications.form_answers` в проде.**
  Расширение: `collectFormAnswers` / `snapshotFormAnswers` перед каждым Continue/Submit. background
  прикладывает копию в обработчике `APPLICATION_SAVED`. Сайт: `AnswersBlock` «What we answered for you».
  Выпущено в 1.8.46. Блок пустой у старых заявок. Кнопку «Change» под ответами делает teal-fox
  (лейн drop-actions, AnswersBlock вынесен в свой файл) → `/profile/facts`.
- **П.3 «Let Drop finish it»: не начат, дизайн ниже.**
- **Ext 1.8.46 (10-09):** синкнут на Рабочий стол (папка была `dataless`, синк её восстановил),
  zip `dist/hiredrop-ext-1.8.46.zip` отправлен в CWS на ревью. У Игоря нужен OFF/ON.

## Последний заход

- Сверил отчёт о 4 багах: заглушек нет. Закрыл тире в резюме (#403).
- П.1: правило «готовность → Yes» отменяет решение 09-30 («UNKNOWN на офис/переезд») только для
  ГОТОВНОСТИ. Факты («ты в NYC?»), дата старта, виза и аттестации не тронуты. Память `logistics-answer-yes`.
- П.2: снимок формы вместо инструментирования пяти филлеров, чтобы показать ровно то, что ушло.
  Схема обрезает, а не отклоняет: копия не может стоить строки заявки (422).

## Сломано / не доделано

- **П.3 дизайн («кампания из одной вакансии», только ATS: GH/Lever/Ashby):**
  1. Сайт `HistoryView.tsx`: кнопка «Let Drop finish it» вместо «Finish form ↗», без подписи.
     `window.postMessage({type:"HIREDROP_FINISH_HANDBACK", handback})` → `ping.js` → background
     (по образцу `HIREDROP_START_CAMPAIGN`, ping.js ~103). Ряд → «Filling…».
  2. background: если идёт обычная кампания → отказ «Drop занят твоим прогоном». Иначе видимое
     окно `focused:true`; storage `finishMode={handbackId,url}`, `atsQueue=[{applyUrl:url}]`,
     `atsPlatform=<ats>`, `campaignRunning=true` (движку нужен этот флаг: 68 проверок в content.js).
  3. content.js `phase_ats`: в finishMode плашка в shadow DOM («Заполняю за тебя, не трогай,
     ~1 мин · шаг N из M»). На стене (капча, GH email-код, leftover required) вместо `handBackJob`:
     плашка «Твой ход 👇» + подсветка, `pendingAtsSubmit` (страница подтверждения запишет заявку
     сама, `_recordPendingSubmitOnce`), без перехода дальше. Без стены робот жмёт Submit сам.
  4. После записи заявки `resolve_for_posting` уже закрывает хендбэк. Конец finishMode: стоп
     мини-кампании, окно закрыть, `finishMode` убрать.
  5. Нельзя забыть: мини-кампания не должна считаться прогоном в дашборде, автостарте, heartbeat
     и капах (см. `campaign-runtime`, `auto-daily-start`), и Stop должен её гасить.
- Indeed/ZR в п.3 не входят: ZR `lk=` открывает пустую панель, у Indeed шаги теряют контекст.
- Ashby yes/no-кнопки на НЕ-логистике (виза и т.п.) по-прежнему никто не нажимает.
- Ashby-фикс #392 и чипсы #308 живьём не проверены.
- Уже собранные резюме в storage держат тире до пересборки.

## Следующий шаг

Модель: **Opus**. Новая сессия берёт лейн drop-finish и делает по порядку:

1. **OFF/ON 1.8.46 у Игоря — только в окно, когда никто не гонит прогон.** Игорь: «не сбивать чужой
   прогон». Сначала `python3 scripts/sessions.py board`: есть живой прогон → ждать его конца. Потом
   попросить Игоря OFF/ON на `chrome://extensions` + перезагрузить дашборд, проверить пинг 1.8.46.
   После первой подачи `form_answers` в строке заявки не пустой.
2. **History: детали заявки — свёрнутые развёртки, наши ответы подсвечены** (Игорь 10-09 дословно:
   «вместо длинного списка всего cover letter resume answers просто развертки, юзер сам нажимает
   и разворачивает, а наши ответы мы отмечаем цветом или подсветкой, чтоб юзер по цвету
   ориентировался»). В `ApplicationDetail` (web `HistoryView.tsx`): «What we answered for you»,
   «Cover letter we sent», «Tailored resume» — каждая свёрнута, заголовок + счётчик, клик
   раскрывает. Ответы робота в развёртке — цветной акцент (палитра крем/чернила, память
   `landing-ink-palette`; обе темы). **Согласовать с teal-fox**: она выносит AnswersBlock в
   `components/dashboard/AnswersBlock.tsx` и добавляет «Change» под ответом. До мержа скрин
   1280/390 в обеих темах (память `design-check-edges`).
3. **П.3 «Let Drop finish it»** по дизайну выше, `blast-radius` до мержа, живой тест на
   GH-форме с email-кодом (стена до Submit), объявить соседям до прогона.
