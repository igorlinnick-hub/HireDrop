# apply-losses — где теряются подачи: хендбэки, потерянные записи, вход в Indeed

Обновлено: 2026-10-06 (ночь, 2) · ветка: main · ext main = **1.8.42** (#371 ZR-дедуп), Рабочий стол синкнут (в Chrome нужен OFF/ON); в Chrome Игоря подтверждён 1.8.41 (пинг 20:54Z) · **CWS: 1.8.41 на ревью; 1.8.42 отклонён ITEM_NOT_UPDATABLE → после одобрения `cws_publish.py ship ../dist/hiredrop-ext-1.8.42.zip`** · web #285 + #289 в проде

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

## Заход 10-05 (поздний): причина review-module найдена

- **Прогон Indeed на 1.8.32** (22:59–23:35Z): открыто 12, подано 8, **4.5 мин/заявку, 13.3/ч**.
  review-module: 8 SUBMIT, **9 no-button**.
- **Гипотеза #333 (fixed-кнопка) НЕ подтвердилась**: `🔘` на отказах — Submit в DOM нет вовсе
  (только навигация, «Save and close», «Report an issue», фиксированное мобильное меню).
  Правка #333 безвредна и остаётся.
- **Настоящая причина — ожидание**: ветка no-button ждала `waitForFormReady(12000)`, а та отвечает
  «готово» на ЛЮБОЙ input в каркасе страницы → ожидание длилось ~1 с (FORM DIAG → abandoned в ту же
  секунду). В успешных визитах Submit появлялся на 2–8-й секунде; черновик A&J Chiropractic брошен
  в 21:37 и подан в 22:59.
- **#335 (смержен, 1e28be9) ext 1.8.33**: `waitForFormButton(15000)` — ждём саму кнопку; строка
  `⏳ button appeared after Ns` при спасении шага; в `🔘` добавлены `heads=[…]` (h1/h2/alert).
  Тест `tests/review-wait-button.test.js` (каркас с input, Submit через 1.5 с), сьюта 32/32.
- Новое: 2 no-button на `contact-info-module` (LeafHome, Church Without Walls) — смотреть после 1.8.33.
- `drive.py` (bc50b54): исход прошлой кампании (`outcome=stopped_by_user`, пустой run-started) драйвер
  принял за конец новой на 1-й секунде и не нажал Stop. Теперь исход засчитывается только со
  штампом старта. Корень на сайте: `CampaignView.tsx:457` (`!runStartedTs ||`) — владелец лейна сайта.

## Последний заход (10-05)

- **Прогон Indeed на 1.8.31** (21:37–22:03Z, `run_history.py --since/--until`): открыто 7, подано 2,
  **12.5 мин/заявку, 4.8/ч** (база 18.9). Потери до формы: fit gate 19, title mismatch 9.
- **#320 работает**: 1 отказ structured-data-review на загруженном PDF (21:44, Bowtech) → дальше
  везде `filled=[indeed-resume]`, SDR больше не встретился. Строка `🧾 sdr resume=file badges=[]`
  — бейджей нет, действия только Add/Edit/Remove: чего ждёт шаг, всё ещё не видно.
- **Главная потеря Indeed теперь — последняя страница**: `review-module` → `btn="-" (none)` →
  «Form abandoned without submit». 10-05: 10 из 12 дошедших до review; с 10-02 — 15 из 18
  (подсчёт по STEP-строкам с `@…review-module`). Все шаги до неё заполнены.
- **#333 (смержен, 84ae853) ext 1.8.32**: `isShownControl()` вместо `offsetParent !== null` в
  `findFormButtonIn`/`isSubmitStep` — fixed-кнопка с меткой submit/continue/review/next и
  реальной рамкой засчитывается (offsetParent = null у самого fixed-элемента). Закреплённый
  «Apply for this job» по-прежнему не берётся. **Это гипотеза** — живую страницу снять не дали
  (клик Apply на аккаунте Игоря заблокирован классификатором). Поэтому же на выходе no-button
  пишется `🔘 @path iframes=N shadows=N btns=[testid:label[op,fx:fixed,box,off]…]` — следующий
  прогон скажет, fixed это, iframe, shadow DOM или кнопки нет вовсе.
  Тест `tests/review-submit-visible.test.js` (7 проверок падают на старом коде), сьюта 31/31.
- `drive.py` (корень, a28021a): Playwright-Chrome соседа (`channel="chrome"`) перехватывает
  AppleScript-цель «Google Chrome» → драйвер падал на 89-й секунде без Stop. Теперь ждёт окно 120 с
  и отказывается стартовать, пока такой Chrome запущен.
- В окне 639094017 осталась вкладка viewjob jk=55486472022d511e (закрыть не дал классификатор).

## Заход 10-02

- **Прогон Indeed на 1.8.30** (21:21–21:46Z, `/tools/run-report`): открыто 4, подано 1, хендбэк 3.
  **Все 3 = загружен PDF (`filled=[resume]`) → structured-data-intro → structured-data-review →
  Continue ×3 → отказ**, без alert/aria-invalid/пустых полей. Единственная подача — без загрузки
  (сразу review-module). Т.е. не «через раз», а каждый раз, когда грузим PDF.
- **#320 смержен (f7aadf3), ext 1.8.31**, 3 скептика (все блокеры закрыты):
  - `preferIndeedResume()` — после первого отказа на structured-data-review (`indeedSdrRefusedAt`,
    14 дней, user-scoped) на resume-selection берём «Use your Indeed Resume» вместо загрузки;
    прогресс только при смене выбора (иначе stall guard не срабатывал → 20 кругов без хендбэка).
  - `structuredReviewSnapshot()` — на отказе строка `🧾 sdr resume=<file|indeed> badges=… acts=… ids=…`
    (только листовые бейджи и 2 слова действий — без текста резюме). Следующий прогон скажет, чего
    ждёт шаг → потом вернуть tailored PDF на Indeed.
  - `forgetHandedBackFromApplied()` (background) — находка jobflow-2f: «applied» ставится ДО Submit,
    хендбэк после клика его не снимал → buildAtsQueue выкидывал вакансию навсегда (7 из 9 GH).
    Теперь перед фильтром очереди снимаем отметки ТОЛЬКО у ОТВЕЧЕННЫХ хендбэков (`requeued_at`).
    Неотвеченные — нет: человек мог дослать руками («введи код и отправь»), снятие = двойная подача.
  - Тесты: `indeed-resume-choice.test.js` (фикстура снята с живой страницы
    `tests/fixtures/indeed-resume-selection.html`), `applied-rollback.test.js`; сьюта 30/30.
- `drive.py` сходит на 98-й секунде: «page has no data-testid» — после Start дашборд на
  `/dashboard/campaign`. Кампания шла; Stop нажат по `data-testid=btn-stop`.

## Сломано / не доделано

- 1.8.31 доехал (пинг 1.8.31) после повторного синка: `~/Desktop/HireDrop-Ext` был dataless (iCloud
  выгрузил Рабочий стол) — перед релоадом `stat -f "%b %Sf"`; может выгрузиться снова. Не делать
  `sync-ext.sh | head` — SIGPIPE рвёт синк.
- ✅ Неотвеченные хендбэки — решение Игоря 10-02: повтор только кнопкой. jobflow-2f откатил #319
  (#322, 603a43f); «Try again» в History (web #243) → `POST /handbacks/{id}/retry` → `requeued_at`
  → наш #320 снимает локальную отметку «подано». Повторный хендбэк сбрасывает `requeued_at`.
  Поле `newer_build` в `/handbacks` — можно показать кнопку и в попапе расширения (не сделано).
- Indeed Resume у Игоря «Created more than a week ago» — после переключения работодатель получит
  его, а не tailored PDF; ответы скринера всё ещё из резюме HireDrop.
- Не блокеры filler-honest — в файле ревью.

## Заход 10-06 00:00Z: 1.8.33 проверена

- **Прогон Indeed на 1.8.33** (23:57–00:09Z, кончились ключи): открыто 7, подано 2, 5.5 мин/заявку,
  10.9/ч. review-module: **0 брошено**; 1 визит спас `⏳ button appeared after 2.5s` (Marketing Director,
  брошен в 21:37). Обе подачи — ранее брошенные черновики (Culinary Khancepts, WhiteWater), обе
  `applied_unconfirmed` (Submit нажат, страница подтверждения не распознана) — проверить, как Indeed
  подтверждает отправку черновика.
- **Ненастоящие jk в пуле**: `0f1e2d3c4b5a6978`, `123456789abcdef0`, `cdef0123456789ab`,
  `fedcba9876543210` — по 3–4 строки `jobs` у 4 юзеров, с НАСТОЯЩИМИ названиями/компаниями; в коде этих
  строк нет. Каждый прогон тратит на них «Dead link» (10-06: 4 из 11 открытий). Похоже на jk-приманки
  Indeed в карточках выдачи → и потеря времени, и сигнал бот-детекции. Лейн сбора (`extractCardInfo`) —
  выяснить источник jk на живой карточке, не чинить денайлистом вслепую.
- drive.py `--platform X` сохраняет выбор в профиль: GH-прогон заменил Игорю indeed→greenhouse;
  возвращено прогоном `--platform indeed` (теперь `indeed|remoteok`).

## Заход 10-06: источник фальшивых jk найден — #341, ext 1.8.34

- **Это ловушки Indeed, не наш код.** Снимок выдачи без логина (встроенный Chromium,
  `marketing coordinator / remote`): 16 карточек, одна — клон соседней настоящей (Doximity) с jk
  `a1b2c3d4e5f67890`, `aria-hidden="true"` + `tabindex="-1"`, высота 0, ссылка `/viewjob?jk=` вместо
  `/rc/clk?…&bb=`, а `span id="jobTitle-9a46b4b76cdc3d37"` — jk оригинала. Человек её не видит; обход
  по `[data-jk]` открывал. В пул попадала первая такая на юзера (уникальность user+link) — отсюда
  «по одной строке на jk у 4 юзеров». Значения меняются (5-е за день) → денайлист бесполезен.
- **#341 (смержен, e301c2d)**: `isDecoyCard()` в `phase1_indeed` — отсекает карточку, если ссылка под
  `aria-hidden` ИЛИ id спана заголовка называет другой jk. Фильтр стоит до `easyApplyCards`, значит
  закрыты и очередь прогона, и `INGEST_JOBS`. Строка `🪤 skipped N decoy card(s) jk=[…]`.
  Фикстура `tests/fixtures/indeed-serp-decoy.html` снята с живой страницы дословно; тест
  `serp-decoy-card.test.js`; сьюта 33/33. По всему снимку при любом наборе селекторов отсекается
  ровно ловушка, 0 из 40 настоящих.
- Старые строки в пуле остались (4 jk × 4 юзера, у 3 — `status=new`); сгорят по одному «Dead link»
  каждая. Руками не чистили. На поиске Houston (welder/CDL) выдача отдала Cloudflare
  «Just a moment…» — снимок только по одному запросу.
- Проверка в следующем прогоне: строки 🪤 есть, «Dead link» на SERP-открытиях ≈0.

- **Прогон Indeed на 1.8.34** (02:05–02:07Z, драйвер): `resuming (ext 1.8.34)`, **🪤 по одной ловушке
  на каждой из 2 страниц** (`abcdef0123456789`, `f1e2d3c4b5a67890` — новые значения), Dead link 0,
  найдено 11 и 17 Easy Apply (настоящие не срезаны). Подано 1 (review-module спасён `⏳ 1.5s`).
  Короткий, потому что роли уже выбрали свою долю дневного капа (13 подач за день) → «all keywords
  searched». Драйвер печатает «online v1.8.33» — это запаздывающий серверный heartbeat, верить строке
  `resuming (ext …)` в логе.

- **Выпуск в CWS 10-06: 1.8.35** (стор был на 1.8.28 → везёт #305/#307/#318/#320/#333/#335/#341/#344).
  Перед выпуском 2 субагента: ревью диффа 1.8.28→1.8.34 на блокеры CWS (нет: ни eval, ни новых хостов/
  permissions) и совместимость прод-бэкенда (все пути ext отвечают ≠404). Ревью нашло should-fix:
  `buttonCensus`/`structuredReviewSnapshot` слали подписи/заголовки без маски почты/телефона → #344
  общий `maskPii()`, тест `diag-mask-pii`. Заметка ревью: `metadata.diag` (диагностика шага) уходит в наш
  activity_log — политика приватности/раскрытие в CWS должны покрывать «page diagnostics on failed steps».
  Zip: `dist/hiredrop-ext-1.8.35.zip` (состав как 1.8.29). Статус — `cws_publish.py status`.

- **GH DoorDash ×2 закрыт — #351, ext 1.8.36.** Причина не в disability-логике: `fillComboboxes` имел
  плоский бюджет 14 проходов (1 виджет за проход), на форме 16 react-select → «combo×14», последние
  демографии не открывались (diag `invalid=[Disability Status]`; `reqEmpty` там — ложная пустота #307).
  Теперь бюджет = 2×виджетов на форме (14…80). Живьём встроенным Chromium на реальной форме с настоящими
  open/click/menu: старый код оставляет пустыми Race/Veteran/Disability, новый — заполняет. Тест
  `combo-pass-budget.test.js` на дословной форме `fixtures/gh-doordash-form.html`. Числовой id `1337`
  у поля — не причина (в content.js везде `CSS.escape`).
- Политика приватности: hiredrop-website #280 (все сайты расширения) смержен, ждёт деплоя Vercel (лимит
  сборок); HireDrop #349 (CI-сверка манифест↔/privacy) смержить после деплоя.

## Заход 10-06 (поздний): кап на компанию — ✅ В ПРОДЕ (#353 c46127c, #359 5921b7d)

- **Правило Игоря 10-06: ОДНА заявка на компанию за 60 дней** (09-30 было 2). Возврат формы
  (хендбэк) тоже занимает слот: DoorDash у Игоря открывали 4 раза за 5 дней.
- **#353** — очередь/колода (`build_queue`): `COMPANY_CAP` 2→1; хендбэк без `requeued_at` держит слот.
  BLOCK скептика р1 («Try again» срезался капом из-за ДРУГИХ хендбэков компании) закрыт: `handbacks.requeued_urls()`
  → `jobs._retried_ids()` (по `_job_key`) → повторённая вакансия обходит кап, идёт первой и занимает слот;
  бар (порог) к ней применяется (иначе живой судья скипает её каждый прогон). Р2 SHIP-WITH-NITS, ниты взяты.
- **#359** — живые обходы Indeed/ZR (раньше кап не проверяли: 6 вторых вакансий у одного работодателя за
  60 дн, 5 на Indeed). `fit_queue.companies_holding_slots()` = одно чтение для всех путей; `/tools/assess-fit`
  отдаёт `skip` ДО судьи (`fit_score: None`, причина «Company cap — …»). Р1 BLOCK: ATS-обход звал assess-fit
  и капал повторённую вакансию обратно → `handbacks.requeued_companies()` снимает кап с компании, где есть
  повторённый хендбэк. Р2: исключение было бессрочным → только requeueable-строки за 14 дней
  (`RETRY_EXEMPT_DAYS`). Пропуски капа = своя потеря «company cap» в run-report, не «fit gate».
  Расширение не трогали. Прод: `/health` 200, роуты на месте.
- Известное: пока открыт повтор (≤14 дн), ВСЕ вакансии этой компании на обходах Indeed/ZR проходят кап
  (судья видит только имя компании, URL нет). Точнее — передавать `job_url` в assess-fit (правка ext).
  На дашборде пропуски капа не видны (`summary()` без счётчика) — только в run-report.
- Продуктовое (Игорю): кадровые агентства (Robert Half, Jobot, Insight Global) и «Confidential» теперь
  1 заявка / 60 дн на Indeed — сколько это стоит предложения, не мерили.
- Не закрыто разведкой: пул/by-link (`/campaign/queue`) и тап-режим кап не проверяют; одобренная строка
  колоды не держит слот (можно одобрить две вакансии одной компании).
- 22 повторные заявки в ОДНУ вакансию (Indeed, 09-02…09-25) — последняя 09-25, свежих нет.
- **Первый блок GH — в порядке (агент, живой прогон настоящего content.js, без отправки):** имя, фамилия,
  почта, телефон (iti, переформатирован в (808) 555-1234), страна, город — заполняются. Прод: First/Last/
  Email/Phone — 0 из 34 GH-хендбэков. Пустую форму Игорь видел в моей пробе / при повторном открытии.
- ✅ **Подписи dropdown на GH — #357 (смержен, 2ebd994), ext 1.8.37.** Было хуже, чем думали: на DoorDash
  10 из 16 списков под чужой подписью (Location → «First Name», School/Degree/вся демография → ""). Фикс:
  `explicitLabel()` внутреннего `[role=combobox]` (aria-labelledby/label[for]) раньше догадки по обёртке.
  Второй слой (поймал тест): визовый вопрос DoorDash содержит «eligibility»/«authorization» → правило
  «eligibility → Yes» в `chooseOption` срабатывало и с верной подписью. Теперь визовые/«работали у нас»
  ответы — только профиль или ИИ, иначе пусто → хендбэк. Скептик р1 BLOCK: верная подпись открыла старый
  баг «authorized … WITHOUT sponsorship» → «No» (= «не имею права работать»), и он же жил в ТЕКСТОВЫХ
  вопросах → `sponsorshipSaysYes()` (5 формулировок корпуса; прочие отрицания → модели). Р2 SHIP-WITH-NITS,
  ниты взяты. Тест `gh-combo-labels.test.js` (20 проверок, 11 падают на старом), сьюта 36/36.
- **Прод (агент, только чтение) — что уже ушло работодателям.** Выбранные значения нигде не пишутся
  (`log()` не идёт на бэкенд); след — `screener_answer_cache.question` = подпись, отправленная ИИ.
  Коллизия подтверждена на GH: Cision, Flexport, oura, tanium, Chime, Snorkel, Cloudflare, Affirm, Later,
  Hightouch, Honor, Muckrack. **Вероятно неверный ответ про визу/право на работу ушёл**: Antonia — Cision
  (09-23, sponsorship=Yes, сильно доказано), Flexport (09-21, authorized=No); Igor — oura (09-24), tanium
  (10-02), modernhealth (10-05, средн.); Dakota (внешний) — Chime (09-28), Snorkel (09-28). У всех троих
  `needs_sponsorship=false`. Lever/Ashby — подписи чистые. Сообщать ли юзерам — решение Игоря.
  Кэш с отравленными ключами (`'LinkedIn Profile*'=>'Yes'`, 14 hits) НЕ чистили: 1.8.37 их не попадёт,
  старым версиям удаление не поможет (они заново спросят ту же бессмыслицу).
- Попутно: **6 GH-подач дошли до /confirmation без строки в `applications`** (Antonia Flexport/Cision,
  Igor masterclass ×2/tia, Dakota Snorkel) — отдельная дыра учёта. Lever Entrata (Dakota): ИИ выбрал
  штат Florida при Иллинойсе (подачи не было); Flexport: `Race => White` выбрал ИИ вместо decline.
  Инструмента «что ответили в дропдаунах» нет — скрипты агента были разовыми.
- `diag.reqEmpty` по-прежнему врёт (Country/Location/Resume/work-auth «пустые», хотя отвечены).
- Политика: web #280 смержен, ждёт деплоя Vercel (лимит сборок); **#349 смержить, когда
  `python scripts/check_privacy_hosts.py` → exit 0**.

## Заход 10-06 (ночь): агентства, LinkedIn-фундамент, две разведки

- **#361 (2a7bff9, в проде):** скрытые имена («Confidential», «Hiring Company», «Stealth»… — `_HIDDEN_EMPLOYER_KEYS`,
  проверка и после board-tail) → `company_key` = "" → не капаются; агентства `AGENCY_CAP = 3` (список +
  маркеры staffing/recruit/personnel/roberthalf). Замер (прод, 60 дн): нац. агентств в пуле 0, местных 3.3% Indeed,
  «confidential» = 10 разных работодателей; новый кап не срезал бы ни одной прошлой заявки.
  `scripts/measure_judge_calibration.py` теперь зовёт боевые `company_key`/`company_cap`.
- **#362 (d30678e) — LinkedIn фундамент, живого поведения нет** → свой хендофф `docs/handoff/linkedin.md`.
  Кап по платформам (`subscriptions.MAX_PER_PLATFORM`, linkedin=5, держит и админа).
- **Пользователям про визу НЕ сообщаем** (Игорь 10-06).
- **Разведка: пустая компания** — Indeed /viewjob берёт имя только из `a[href*="/cmp/"]`; у работодателей без
  страницы её нет → "" (доля строк fit с пустой компанией 0% → 16%/нед к 09-28; 10 applications без company).
  Карточка выдачи имя знает (`pendingJobs[].company`), страница его не берёт. ZR: `a[href*="/co/"]` даёт
  «Learn more about Xexternal». ✅ **#365 (1.8.39)**: фолбэк на карточку ТОЛЬКО по jk, селекторы в jobRoot,
  ZR = карточка по uuid → пул → aria-label/текст ссылки. Скептик: регрессии нет; починено до мержа — последний
  ZR-фолбэк мог дать голое «Learn more about» (= один company_key на всех работодателей ZR → кап блокирует всех).
  Хвост: старые ZR-строки с мусорным именем не совпадут с чистым для капа 60 дн (по одной лишней заявке).
- **Разведка: 6 GH-подач без строки applications** — 5 из 6 = сборки < 1.8.22 (#277 уже чинит; стор 1.8.37 →
  автообновление). Открытый баг (Snorkel, Dakota): MutationObserver зовёт runPhase без проверки вкладки
  кампании (~6423), после submit-релоада init ушёл в «Staying idle». Последствие хуже учёта — **повторные
  подачи** (masterclass/8174068 ×2, tia ×3). ✅ **#366 (1.8.40)**: гейт вкладки кампании в runPhase +
  `pendingAtsSubmit` до клика → запись на /confirmation до любых гейтов, ровно одна. Скептик: ни один законный
  путь не замолк. Починено до мержа: (а) «ремень» сдвигал очередь через APPLICATION_SAVED, а обход слал ещё
  ATS_JOB_DONE → следующая вакансия выпадала и в пуле PATCH'илась `skipped`; теперь `advance:false` у ремня,
  `recorded:true` у обхода; (б) подсказка подтверждения = целый сегмент пути (`/appliedintuition/` давал
  фальшивую подачу). 6 строк задним числом не вносили (данные юзеров — решение Игоря).
- **✅ ZR дедуп по пути URL — починено #371 (1.8.42).** Ключ сессионного дедупа на странице вакансии был путь
  `/jobs-search/N` (вакансия = `?lk=<uuid>`) → все вакансии страницы кроме первой «already processed». Теперь
  `zrDedupeKey` = lk-uuid, как у списка; «already processed» идёт в logBackend. Следствие: ZR подаёт больше за прогон
  (потолок — `MAX_APPLICATIONS_PER_PLATFORM`=15 + доля ключевика; бан-риск потолка не замерен). Проверить живым ZR-прогоном.

- **Живой прогон 10-06 07:46Z (`drive.py run auto`, все платформы):** в Chrome 1.8.40 подтверждён строкой
  `resuming (ext 1.8.40)`; обход Indeed пошёл (поиск → 15 Easy Apply → 2 /viewjob, вкладка кампании
  автоматизировалась — гейт #366 её не заглушил). Через ~2 мин Mac уснул (крышка закрыта, `AppleClamshellState=Yes`;
  драйвер сам не тикал 114→467 с) → «asleep ~6 min» → Stop. Подач 0, вывода про #365/#366 по подачам нет —
  повторить прогон при открытой крышке.

- **Прогоны 10-06 ~20:00Z на 1.8.40.** (1) GH-only (`--platform greenhouse`, 4 в очереди): Grafanalabs подан →
  «recorded on the confirmation page» → «already recorded, next job» → следующей открылась 4-я (Hightouch,
  закрыта) → «run complete». В applications ровно 1 строка. **#366 подтверждён живьём**: одна запись, один сдвиг.
  (2) Indeed (all connected): ext ждёт Cloudflare «Just a moment» — 3 загрузки /viewjob ровно через 64 с
  (60 с ожидания + перезагрузка ×2), затем «Detection tripped» → хендофф капчи. Не регрессия: CF-стена на
  Indeed для этого браузера (10-05 23:05 то же на /jobs). Дыра наблюдаемости: ожидание CF пишет только `log()`
  → в проде 3 минуты выглядят как зависание; поднять «Cloudflare check — waiting» в `logBackend` (content.js
  ~L6330). #365 живьём не проверен (на Indeed до оценки не дошло).
- **drive.py:** (а) читал URL из стартовой строки «Opening the automation window → secure.indeed.com/auth?continue=»
  как стену логина и гасил прогон на 1 с — починено (корень, 2c5e2c6, +кейс в test_diagnose.py);
  (б) `open` «адоптирует» окно, в котором была hiredrop, даже если оно теперь окно автоматизации на /viewjob
  → «btn-ext-reload not found». Не чинил; обход — закрыть окно автоматизации после прогона.

- **Смержено 10-06 ночью:** ext #369 (1.8.41, logBackend в ожидании Cloudflare «Just a moment», тест
  `tests/cf-wait-visible.test.js`) → `dist/hiredrop-ext-1.8.41.zip` → `cws_publish.py ship` OK. web #285 (чек-лист
  не мигает «Turn the extension back on» при перезагрузке ext; честное «выключено» через ≈10.5 с). Не проверено
  скептиком/вне PR: Start-гейт (StartReadiness.tsx:163) пробует один раз 1.5 с — при перезагрузке ext может сказать «install».
- **drive.py (корень, локальный репо без remote):** 2c5e2c6 — стартовая строка с `secure.indeed.com/auth?continue=`
  больше не «стена логина»; 7582546 — exec_js бьёт во вкладку hiredrop окна, а не в активную; open_window сначала
  берёт окно, где дашборд — активная вкладка (проверено живьём: чужая активная вкладка не мешает).

## Следующий шаг

Модель: **Opus**. **Волна 1 (10-06 ночь) смержена:** #373 `scripts/handback_reasons.py` · #374 ATS-очередь берёт
серверный fit (11 ложных отсевов с 09-30) · #375 отказ «Review your resume details» → повтор с Indeed Resume, в лог
текст ошибки Indeed · #376 + web #291 ежедневный автостарт (память `project_auto_daily_start`; Start больше не глотает
403) · #377 хендбэк Indeed = вакансия, не шаг (терялось 10 из 15) · #379 починил main после #375+#377.
**В полёте:** #378 ответы «authorized/sponsorship» (2-й ремонт: регрессия Twitch «permanent resident» → Yes) · ветка
`ext-title-prefilter-cards` (фильтр названия на карточках). **Агенты, работавшие в момент /clear (их отчёт в новую сессию НЕ придёт — сверять по GitHub):** #378 после 2-го
ремонта (fd9e005: Twitch → пусто, «permanent basis» (T,T) → No, «citizen or green card» → пусто; автор: 240 ячеек, test:ext 46/46, pytest 1251, на реальных схемах ни одного «прав → неправ») → нужен повторный скептик (скрипты скептика: scratchpad прошлой сессии `rv/radio_real.js` — пропадут, писать заново;
критерий: на 320 реальных GH-схемах у гражданина нет ни одного «был прав → стал неправ») · фильтр названий — PR
`ext-title-prefilter-cards` (если открыт → скептик → мерж). Перед каждым мержем `gh pr update-branch` + CI на свежем main.
Worktree'ы удалить после мержей: `.wt-start-gate .wt-zr .wt-handback-reasons .wt-fit-reuse .wt-indeed-resume
.wt-handback-identity .wt-auto-daily .wt-web-auto-daily .wt-walker-url .wt-work-auth .wt-title-prefilter` (классификатор режет удаление — Игорю).
**Релиз:** когда CWS одобрит 1.8.41 → бамп 1.8.43 (всё
выше + #378) → `sync-ext.sh` → zip → `cws_publish.py ship`. Web #291 до этого показывает «Update the extension».
**Ждёт Игоря:** год окончания в профиле (причина отказа Indeed: education без даты — у Игоря и 2 юзеров) · порог fit
35/32 · поле street address (3 хендбэка Indeed) · живые прогоны Indeed/ZR («давай»).

Файлы лейна: `chrome-extension/content.js` (waitForFormButton перед waitForFormReady; isShownControl/buttonCensus после findFormButtonIn ~L3970; no-button ветка ~L4690, preferIndeedResume/structuredReviewSnapshot ~L3860, step loop ~L4325), `background.js` forgetHandedBackFromApplied ~L1059, `tests/indeed-resume-choice.test.js`, `tests/applied-rollback.test.js`, `chrome-extension/content.js` (formBlockers ~L3500, fillTextQuestions ~L2990,
pay/school helpers перед isDemographicQuestion, fillComboboxes, fillCheckboxes),
`background.js` ATS_JOB_FAILED, `tests/form-blockers.test.js`, `tests/filler-honest.test.js`.
