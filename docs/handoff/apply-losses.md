# apply-losses — где теряются подачи: хендбэки, ответы в формах, обход Indeed/ZR

Сессия: maple-stoat · лейн ext-zr-truth · цель: ZR 1-click заявки пишутся в History (не Skip) и с городом; проверено живой ZR-заявкой · шаг: #398 в main (1.8.45), синк на Рабочий стол ✅; живая ZR-заявка заблокирована классификатором прав → ждёт разрешения Игоря · доска: `python3 scripts/sessions.py board`

Обновлено: 2026-10-09 · ветка: main · ext main = **1.8.45** (#398) · CWS: 1.8.43 на ревью. Хронология — `git log -p` этого файла.

## Состояние

- Живой Indeed на 1.8.43: 3 подачи / 12 мин, все в `applications`; #381 #382 кап компании, fit-отсев — подтверждены вживую.
- **ZR (#398, 1.8.45, в main и на Рабочем столе Игоря, вживую НЕ проверен):** экран ZR после подачи
  (`Send a Message | Skip for Now`, `bsf-*`, «has been submitted») = заявка → `recordSubmittedApplication`; выход —
  «Skip for Now», «Send a Message» в `DENY_BTN_RE`. Панель «Applied» считается только без открытых окон (пустая
  Close-оболочка ZR между шагами ≠ конец). Сигнал читается ДО Stop. `_phase3_fillForm` спрашивает ZR в начале шага и
  перед каждым отказом (`zrFiledDuringForm`). Пагинация выдачи — не кнопка формы (`isPageNavControl`), кнопки модалки
  ZR ищутся только в модалке (`formLivesInDialog`). Город: карточка `job-card-location` + шапка панели → `/jobs/describe`.
- Защита от ложного «подано» проверена фикстурами + 2 скептиками; экран после подачи никогда не снимался — его
  разметка из бандла ZR, доказательство только живой заявкой.

## Последний заход (10-08…09, maple-stoat)

- Доделал брошенную `.wt-zr-truth` → #398: город, многошаговые формы, флаг модалки, 9 находок скептиков, стандарты кода
  #393 (комментарии без истории, один `isShownDialog`, тесты вызывают код: `zr-submit-truth.test.js` гоняет настоящие
  `phase2_ziprecruiter` и `_phase3_fillForm` через Proxy-песочницу).
- Blast radius: `isDeniedFormButton`/`visibleApplyDialogs` старый vs новый по всем фикстурам — у Ashby/GH/Indeed 0 изменений.
- `drive.py run auto --platform ziprecruiter` **заблокирован классификатором прав** (Real-World Transactions) — живую
  подачу запускает только Игорь или разрешение в настройках. `profiles.platforms` до прогона: igor = `["indeed","remoteok"]`.
- #401 (сессия teal-fox) тоже бампал 1.8.45 и правит content.js — оставлен коммент: rebase + 1.8.46.

## Сломано / не доделано

- ZR вживую не проверен (см. выше). Низкие риски скептика оставлены: нет проверки экрана «подано» ДО клика;
  старый поиск кнопки Apply по всей странице. ZR-харвест в пул шлёт без города (город доходит только при подаче).
- #384, #385 открыты (#385 пересекается с #398 в ветке `if (!formReady)` — при ребейзе оставить запись `applied` выше).
- `screener_answer_cache` раньше `_status_from_profile`; `diag.reqEmpty` врёт; worktree'ы `.wt-*` удалить (Игорю).

## Следующий шаг

Модель: **Opus**. Получив разрешение Игоря на живую подачу: `drive.py run auto --minutes 20 --platform ziprecruiter`,
остановить на первой ZR-строке в `applications` (`scripts/e2e/sql.sh`), проверить в History место (не «No location»),
вернуть `profiles.platforms` igor = `["indeed","remoteok"]` → `zip` + `cws_publish.py ship` после одобрения 1.8.43.
