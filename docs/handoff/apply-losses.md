# apply-losses — где теряются подачи: хендбэки, ответы в формах, обход Indeed/ZR

Сессия: brisk-toucan · лейн ext-zr-truth · цель: ZR 1-click заявки пишутся в History (не Skip) и с городом; проверено живой ZR-заявкой · шаг: ZR доказан живой заявкой (applied + San Diego, CA); профиль вернул; хендофф + мерж #406 · доска: `python3 scripts/sessions.py board`

Обновлено: 2026-10-09 · ветка: main · ext main = **1.8.46** · Хронология — `git log -p` этого файла.

## Состояние

- Живой Indeed на 1.8.43: 3 подачи / 12 мин, все в `applications`; #381 #382 кап компании, fit-отсев — подтверждены вживую.
- **ZR ДОКАЗАН ЖИВОЙ ЗАЯВКОЙ (10-09, brisk-toucan):** `drive.py run auto --platform ziprecruiter`, ext 1.8.46 в content
  script. Первая подача через ~2 мин → `applications` 3d46289f: Boutique Fitness Studio, «Sales & Operations Manager»,
  status `applied`, `jobs.location` = «San Diego, CA • On-site» → History `place` = «San Diego, CA», `onsite`.
  Stop с первого раза. `profiles.platforms` igor вернул в `["indeed","remoteok"]`. Механика (#398): экран ZR после подачи
  (`Send a Message | Skip for Now`, «has been submitted») → `recordSubmittedApplication`; город — карточка + шапка → `/jobs/describe`.
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

- Низкие риски скептика по ZR оставлены: нет проверки экрана «подано» ДО клика; старый поиск кнопки Apply по всей
  странице. ZR-харвест в пул шлёт без города (город доходит только при подаче).
- ⚠ `drive.py run` синкает расширение из ЛОКАЛЬНОГО `jobflow/` (не origin/main): когда checkout отстаёт, синк ОТКАТЫВАЕТ
  Рабочий стол на старую версию. 10-09 так чуть не откатил 1.8.46 → 1.8.45. Обход: гонять drive.py из копии с
  `git archive origin/main chrome-extension`. Корень — в sync-ext.sh/drive.py брать источник по коммиту, не по checkout.
- Диск был 99% → синк на Рабочий стол (iCloud) вис. 10-09 освобождено до 67 ГБ; папку расширения всё ещё стоит увести
  из iCloud в `~/Code/` (один «Load unpacked» у Игоря).
- #384, #385 открыты (#385 пересекается с #398 в ветке `if (!formReady)` — при ребейзе оставить запись `applied` выше).
- `screener_answer_cache` раньше `_status_from_profile`; `diag.reqEmpty` врёт; worktree'ы `.wt-*` удалить (Игорю).

## Следующий шаг

Модель: **Opus**. Лейн ext-zr-truth по цели закрыт. Дальше по выбору Игоря: (1) источник синка по коммиту (грабля выше),
(2) папка расширения вне iCloud, (3) риски скептика по ZR. Статус ZR в `STATUS_MATRIX.json` уже VERIFIED — не трогать.
