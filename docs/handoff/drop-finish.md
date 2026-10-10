Сессия: hazel-yak · лейн drop-finish · цель: Хендбэк: «Let Drop finish it» перезаполняет форму в видимом окне до стены; логистика → Yes; копия Q&A в History · шаг: п.3: PR #424 (ext 1.8.48) + web #334 открыты, ждут ревью; живой тест после прогона coral-merlin и «давай» Игоря · доска: `python3 scripts/sessions.py board`

# drop-finish — хендбэк доводится роботом, человек делает один шаг

Обновлено: 2026-10-09 (hazel-yak) · ветки: `feat/drop-finish-handback` (HireDrop #424), `feat/drop-finish-button` (web #334)

## Состояние

- **П.1 логистика → «Yes»** и **П.2 копия Q&A в History**: в проде, ext 1.8.46+. У Игоря в Chrome
  **1.8.47** (перезагрузила `HIREDROP_DEV_RELOAD`, пинг проверен на `?dev=1`). `form_answers` у старых
  заявок пустой, наполнится с новых.
- **Развёртки в карточке заявки: web #332 в проде.** `Fold` в `HistoryView.tsx`: ответы, письмо,
  резюме свёрнуты; ответы робота с маркером `.hd-ours`, исправленные через «Change» маркер теряют.
- **П.3 «Let Drop finish it»: код готов, PR открыты, НЕ смержены.**
  - ext #424 (1.8.48): `ping.js` `HIREDROP_FINISH_HANDBACK` → bg `FINISH_HANDBACK` →
    `startFinishRun` (фокусное окно about:blank → состояние → навигация; очередь из одной вакансии,
    `atsPlatform:"pool"`, маркер `finishRun`). На стене `handBackJob` → `finishWall` (content.js):
    плашка в closed shadow root, обводка, `pendingAtsSubmit` с ttl 30 мин, `FINISH_WALL` → bg
    `endFinishRun` (окно остаётся), затем `waitForSubmissionConfirmation` на 30 мин пишет
    подтверждение без смены URL. Без стены: `APPLICATION_SAVED` → `advanceAtsQueue({sent:true})`
    → окно закрывается.
  - Не кампания: пинг `campaign_running:false` и игнор `should_run`; `atsWalkWatchdog` и
    `tapPoolIdleRefill` молчат; `autoDailyTick` ждёт; `startCampaign` перехватывает; STOP чистит;
    `ATS_JOB_DONE` не PATCH-ит skipped; закрытие окна → `endFinishRun`.
  - web #334: `useFinishHandback.ts` (кнопка только для GH/Lever/Ashby на их хостах; «Filling…»;
    отказы фиксированным текстом: busy / daily_limit / old_extension (есть PONG) / no_extension;
    через 20 с кнопка возвращается, ссылка «Finish form ↗» на месте при отказе).
  - Тесты: `chrome-extension/tests/finish-run.test.js` (27 проверок, §0 доказывает, что обычная
    кампания не изменилась), весь набор 60/60; web tsc/eslint/129 тестов.

## Последний заход

- Карта движка ATS (агент): глобальные синглтоны run-state, heartbeat убил бы мини-прогон за ~60 с,
  метки applied после стены после клика, сторож перезагрузил бы страницу под человеком — всё учтено.
- Blast radius: факт безопасности доказан ступенью 4 (тест §0). Перекос версий закрыт на сайте
  (PONG отличает старое расширение). Хосты/manifest не менялись, бэкенд не трогали.
- Адверсариальное ревью обоих диффов вернулось: 4 блокера, список в «Сломано».

## Сломано / не доделано

**Ревью #424/#334 получено: 4 блокирующих бага, НЕ МЕРЖИТЬ до починки.**
1. Start во время заполнения: `startCampaign`→`endFinishRun` оставляет окно, его `phase_ats` идёт
   дальше на флаге НОВОЙ кампании; на стене `currentFinishRun()` уже null → обычный `handBackJob`
   → `ATS_JOB_FAILED` PATCH-ит skipped голову новой очереди и сдвигает её. Фикс: передать `finish`
   из входа `phase_ats` в `handBackJob` и молчать при смене id; в bg игнорировать
   `ATS_JOB_DONE`/`ATS_JOB_FAILED`/advance из `APPLICATION_SAVED`, если `sender.tab.id !== campaignTabId`.
2. Обратная гонка: Stop посреди кампании → «Let Drop finish it»: `phase_ats` старой кампании
   (finish=null) проходит `isCampaignRunning()` на флаге finish и подаёт старую вакансию, её
   `APPLICATION_SAVED` закрывает окно finish. Фикс: тот же sender-tab чек + run-токен для каждого `phase_ats`.
3. Кап на платформу не проверяется в `startFinishRun`: `phase_ats` шлёт `STOP_CAMPAIGN`
   («stopped_by_user» + `/campaign/stop`), окно с «Hands off» над пустой формой. Фикс: проверить
   `platformCounts[platform]` против per-platform капа → `daily_limit`.
4. `applied_unconfirmed` тоже закрывает окно (`advanceAtsQueue({sent:true})`) и закрывает хендбэк.
   Фикс: в finish-run при `!result.verified` → `finishWall` («Check the form and press Submit»),
   окно закрывать только при verified.

Риски (чинить там же): 5) `finishRun` остаётся висеть на путях без `endFinishRun` (429 в
`APPLICATION_SAVED`, `noteAuth401` закрывает окно, `onInstalled`) → автостарт вечно «busy»; в пинге
завершать при `finishRun && (!windowAlive || !campaignRunning)`, чистить в onInstalled/noteAuth401.
6) подача человеком на стене не считается локально (`recordLocalApplication`). 7) `startFinishRun`
ждёт `addToActivityLog` (сеть) до ответа → сайт может показать «нет расширения»; не await-ить лог.
8) `captureActiveAutomationTab` цепляет CDP к окну человека → вернуть false при finishRun.
9) free-лимит не проверяется до старта. Сайт: 10) у finishable-строки может не быть входа (ссылка
только при отказе) → ссылку показывать всегда рядом с кнопкой; 11) утечка `onPong`, `unsupported`
с неверным текстом. Мелочи: 12) плашка «Hands off» висит после ранних выходов/Stop; 13) 30-мин
`pendingAtsSubmit` может перезаписать следующая кампания.
Тесты: `finish-run.test.js` не покрывает 1–5 → добавить сценарии вместе с фиксами.

- **Живой тест (ступень 5) не делался.** Нужен «давай» Игоря и окно без чужого прогона
  (coral-merlin гонит Indeed ~30 мин; ждать её «прогон закончен»).
- Ashby yes/no-кнопки на не-логистике по-прежнему не жмутся; Ashby #392 и чипсы #308 живьём не проверены.
- Worktree'и этой сессии в scratchpad (`be`, `web`): ветки запушены, локально можно удалить.

## Следующий шаг

Модель: **Opus**. 1) Починить баги 1–4 + риски 5–11 из «Сломано» в ветках #424/#334 (`git checkout feat/drop-finish-handback` в worktree), тесты на сценарии 1–5, повторное ревью. 2) После «прогон
закончен» от coral-merlin и «давай» Игоря: `sync-ext.sh` из ветки #424 на Рабочий стол (`stat` папки —
iCloud dataless), `HIREDROP_DEV_RELOAD`, проверить 1.8.48 на `?dev=1`, нажать «Let Drop finish it» на
одной GH-хендбэк Игоря **с email-кодом** (стена после клика, без кода ничего не уходит), снять экран
плашки. 3) Мерж #424 → web #334 → `cws_publish.py` 1.8.48. После теста вернуть Рабочий стол на main,
если мерж откладывается.
