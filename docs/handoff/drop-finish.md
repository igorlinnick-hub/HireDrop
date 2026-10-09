Сессия: calm-quail · лейн drop-finish · цель: Хендбэк: «Let Drop finish it» перезаполняет форму в видимом окне до стены; логистика → Yes; копия Q&A в History · шаг: п.1 и п.2 в main, п.3 спроектирован · доска: `python3 scripts/sessions.py board`

# drop-finish — хендбэк доводится роботом, человек делает один шаг

Обновлено: 2026-10-09 · ветка: main

## Состояние

- **П.1 логистика → «Yes»: в main (#410), у юзеров НЕТ.** Бэкенд `_willing_yes`
  (`modules/ai_question_answer.py`) работает до модели, но после личного ответа человека
  (personal_facts, #401). Расширение: `isWillingnessQuestion` + `fillCheckboxes` + Ashby yes/no-кнопки.
  Тест на живой разметке Suno.
- **П.2 копия Q&A в History: в main (#411 + web #327), колонка `applications.form_answers` в проде.**
  Расширение: `collectFormAnswers` / `snapshotFormAnswers` перед каждым Continue/Submit. background
  прикладывает копию в обработчике `APPLICATION_SAVED`. Сайт: `AnswersBlock` «What we answered for you».
  **У юзеров НЕТ**, пока расширение не выпущено. Блок пустой у старых заявок.
- **П.3 «Let Drop finish it»: не начат, дизайн ниже.**
- Расширение: main = 1.8.45 + п.1 + п.2 без бампа. Релиз (бамп → sync → CWS) ждёт окончания
  контрольного прогона plum-salmon и OFF/ON 1.8.45 у teal-fox.

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

Модель: **Opus**. Выпустить расширение с п.1+п.2, когда соседи закончат (бамп → sync → OFF/ON →
`cws_publish.py`). Затем п.3 по дизайну выше, `blast-radius` до мержа (content.js, background,
ping.js, очередь). Живой тест на GH-форме с email-кодом: стена до Submit, без отправки чужим.
