# apply-losses — где теряются подачи: хендбэки, ответы в формах, обход Indeed/ZR

Обновлено: 2026-10-06 (ночь) · ветка: main · ext main = **1.8.43** (#378 #381 #382 #383) · **CWS: 1.8.43 отправлен на ревью**
(1.8.41 раздаётся). Длинная хронология 09-30…10-06 — `git log -p docs/handoff/apply-losses.md`.

## Состояние

- Живой Indeed на 1.8.43 (22:42Z): 3 подачи / 12 мин, все в `applications`. Вживую подтверждены #381 (карточки
  отсеиваются на выдаче, строка «Skipped N of M cards»), #382 (ротация ключевиков), кап компании, fit-отсев.
- **ZipRecruiter сломан тихо:** Quick Apply подаёт в один клик/после одного Continue, ZR показывает диалог
  `Close | Send a Message | Skip for Now`, а мы пишем «Skip (no ZR form after 40s)» или хендбэк → в `applications` 0 строк
  (с 09-06 записана 1 ZR-заявка). Ground truth — ziprecruiter.com/candidate/my-jobs («Applied Today» ×3 за 10-06:
  Equation Events, MLW Hire, Crush Innovations — НЕ внесены, решение Игоря). Плюс степ-луп кликал пагинацию выдачи
  «next page» ×10 как кнопку формы. Масштаб мал: ZR гонял только Игорь (6 кликов за 2 дня).
- drive.py: `open` подхватывает окно автоматизации прошлого прогона → «btn-ext-reload not found»; обход — закрыть
  окна автоматизации по id перед прогоном. `verify.sh` «no rows» врёт (заявки были). `--platform` пишет
  `profiles.platforms` — вернул `['greenhouse','remoteok']`.

## Последний заход (10-06 ночь)

- Смержены #382 (ledger капа ключевика со своей датой и ключом-фразой — вчерашние доли гасили прогон),
  #381 (+ `run_report` считает «Skipped N of M cards» как N), #378 (5 раундов скептика; 0 «прав→неправ»).
- Открыты, скептики работали в момент /clear (отчёты в новую сессию НЕ придут):
  **#384** Indeed: Veteran Status (decline-опция с кривым апострофом ’ не узнавалась) + лимит «shorter than 100
  characters» (обрезка своих ответов по границе фразы); агент заявил, что main на радиогруппах Indeed читал 1-ю
  опцию как вопрос и мог выбрать «Male» — **проверить**. **#385** вкладка кампании: адопция только smartapply/auth от
  кампанийной вкладки, `RECLAIM_CAMPAIGN_TAB` на «no form after Apply»/ZR «no form», idle-вкладка перепроверяет
  (156 таких зависаний с 09-01). Ветка **`ext-zr-submit-truth`** — агент чинил ZR (успех по post-apply диалогу,
  запрет пагинации как шага); PR мог не открыться — сверить `gh pr list`; пересекается с #385 в ZR no-form пути.

## Сломано / не доделано

- ZR — см. выше; пока не смержен фикс, ZR-прогоны не гонять.
- `screener_answer_cache` отдаётся раньше `_status_from_profile` (старые «Yes» на «legally work…», 80 строк).
- `diag.reqEmpty` врёт. Worktree'ы `.wt-*` в `~/Code/JobFlow/` удалить после мержей (Игорю — классификатор режет).

## Следующий шаг

Модель: **Opus**. Скептики заново на #384, #385 (blast-radius: background.js адопция) и PR ветки `ext-zr-submit-truth`
(если не открыт — допилить из worktree `.wt-zr-truth`) → мерж (`gh pr update-branch` + CI) → бамп 1.8.44 →
`scripts/sync-ext.sh` → живой ZR и Indeed (`drive.py run auto --platform …`; «давай» есть; окна автоматизации закрыть
по id до старта) → zip (файлы `chrome-extension/` без tests, см. состав 1.8.43) → `cws_publish.py ship` после одобрения
1.8.43. **Ждёт Игоря:** внести 3 ZR-заявки задним числом? · год окончания · порог fit 35/32 · street address.
