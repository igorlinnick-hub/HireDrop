# drop-actions — Drop с кнопками, память ответов о себе, наблюдение за Drop

Сессия: hazel-ocelot · лейн drop-actions · Обновлено: 2026-10-10 · ветка: main · в работе: — · ждёт: «да» Игоря на карточку «сменить режим» + удаление уточнителя; недельный замер 10-16

## Состояние

- **Уточнитель, шаг 1 — в проде** (#426, `c71ed81`): раз в день Drop выбирает ОДНУ спорную вакансию
  (|оценка − планка| ≤ 15, текущий вердикт, статус new) и спрашивает шаблоном:
  «Close call: <job> at <co> (<where>) just missed your list. Worth applying? 0 to 10, or 👍 👎.»
  («barely made your list», если в списке). Текст утвердил Игорь 10-09 («короче и ближе к сути»).
  Ответ пишет инструмент `record_fit_answer`, в аккаунте пока ничего не меняется.
  `GET /buddy/clarify`, `POST /buddy/clarify/{id}/seen`, `AskBody.clarify_id`, событие `clarify`,
  `scripts/clarify_report.py`. Таблица `fit_clarifications` + уникальный `(user_id, job_id)` —
  **обе миграции применены в проде**. Юзеры ничего не видят: сайт эндпоинты не зовёт.
- **Что держит честность**: «made your list» говорится только о вакансии из настоящего списка
  (`_deck_queue`, общий с `GET /jobs/deck`); открытый вопрос перепроверяется (`still_stands`:
  статус new, та же сторона планки) — иначе молчим до завтра; одна вакансия — один вопрос навсегда.
- **Раньше в проде**: бэкенд #401 (факты о человеке), сайт web #325 (карточки Drop, 👍/👎, скрепка,
  `?app=`, «About you»), web #328 («Change» в History). Логистика → «Yes» без вопросов (#410, drop-finish).
- **Расширение**: 1.8.45 у Игоря, в CWS на ревью с 10-09; 1.8.46 делает лейн drop-finish.
- **Наблюдение**: «3в. Drop вчера» в prod-sweep; launchd `com.igor.drop-week-review` 2026-10-16 09:25
  (отчёт → `docs/reviews/2026-10-16-drop-week.md` корня).

## Последний заход

- hazel-ocelot (10-09): текст вопроса короче (Игорь) → 2 скептика (A: роутер/БД/IDOR, B: выбор/честность)
  → починено 6 находок с тестами (ложное «made your list» при капе компании/лимите 30; открытый вопрос
  не перепроверялся; гонка двух вкладок = 2 вопроса, доказана 3 потоками; `"]` в названии закрывал
  заметку для модели; нечитаемый старт дня снимал кап; ответ затирал `seen_at`) → индекс в прод →
  живая проверка локальным кодом под `+buyer1` (3 загрузки → 1 вопрос; Drop записал «6» со словами;
  Q2 — другая вакансия «barely made», она в списке) → #426 CI зелёный → мерж → Railway → прод-запрос
  под `+buyer1` вернул тот же вопрос дважды. Тестовые строки удалены (0). pytest 1744.
- Оставлено (мелочи): пропуск из постороннего сообщения держится на промпте; сырые поля вакансии
  в `public()["job"]` — данные для карточки; при сбое чтения пояса день по UTC (как у капа).

## Сломано

- Ничего нового. Мелочи прежние: Drop называет `date_applied` полночью UTC в поясе юзера
  («Oct 8, 2:00 PM» на Гавайях, `modules/buddy_facts.py`); вопрос длиннее 200 символов не совпадёт
  при правке из History; факты одним jsonb (≤40); `openPopup()` — Chrome 127+.

## Следующий шаг — ждёт «да» Игоря: карточка «сменить режим» + убрать уточнитель

Модель: **Opus**.

**Решение Игоря 10-09, уже после мержа #426: юзера про спорные вакансии НЕ спрашиваем.** Вопрос
выглядит так, будто мы не уверены в своей системе, а планку человек уже выбрал режимом
broad / standard / precise (память `project_clarifier_closed`). Шаги 2 (пузырь на сайте) и 3 (скрыть
такие / подать на неё, сдвиг личной планки) **сняты**. Не предлагать заново.

**Предложено Игорю 10-09 и 10-10, ждёт «да»:**
1. **Карточка Drop «сменить режим»** — только когда юзер сам просит («хочу точнее» → Precise,
   «мало вакансий» → Broad); режим меняется только кнопкой. Только бэкенд: новый `kind` в
   `modules/buddy_actions.py` (`KINDS` + ветка в `build()`, стр. 100), шаг
   `POST /profile/apply-mode {"apply_mode": ...}` (`app/routers/profile.py:203`). Сайт уже исполняет
   `steps` любой карточки (web #325), его не трогать. Учесть: смена режима меняет `verdict_version`,
   список пересуживается (как при смене в Settings); уход с Precise стирает `ideal_job_description`
   (`app/db/profile.py:269`) — сказать на карточке; переход на Precise без текста оставляет прежний.
2. **Убрать код уточнителя** (#426): `modules/fit_clarify.py`, `app/db/fit_clarify.py`,
   `/buddy/clarify*` и `AskBody.clarify_id` в `app/routers/buddy.py`, `record_fit_answer` и событие
   `clarify` в `modules/buddy.py`, `scripts/clarify_report.py`, правило в `scripts/delete_account.py`,
   `tests/test_fit_clarify.py`. `_deck_queue` в `app/routers/jobs.py` оставить (им пользуется дека).
   Таблица `fit_clarifications` пустая → `drop table` миграцией через supabase CLI. Код никто не
   зовёт (grep по сайту и расширению = 0), срочности нет.

10-16: прочитать отчёт launchd (`docs/reviews/2026-10-16-drop-week.md` корня), решить про дату в Drop.

## Контракт для сайта (hiredrop-website) — пока НЕ сделан

Клиент чата уже игнорирует неизвестные события (`lib/buddy.ts` 44–53), поэтому бэкенд можно
катить раньше сайта.

- **`lib/buddy.ts`**:
  - Добавить обработку `{"type":"proposal","proposal":{id, kind, title, lines[], confirm,
    steps[], navigate, note, done}}` → `on.onProposal(card)`.
  - `done` и `error` теперь несут `turn_id` — сохранять его в сообщении Drop.
  - В тело запроса добавить `cards: true`, как только карточки рисуются. Без этого флага Drop
    не получает `propose_action` и, как раньше, говорит, куда нажать. Так бэкенд безопасно
    катить раньше сайта.
  - Когда PDF загружен из чата — добавить `attachment: "resume_pdf"`.
  - Мелкая ошибка: после цикла не разбирается последняя строка без `\n`. Буфер стоит дочитать.
- **Карточка в `BuddyPanel`**: заголовок, `lines`, `note`, кнопка `confirm` и «Not now».
  - Нажатие выполняет `steps` ПО ПОРЯДКУ: `{method, path, body}` к `/api/v1` с Bearer.
    Нужен общий `request(method, …)`, потому что DELETE в `lib/api.ts` нет.
  - Если есть `navigate`, нажатие делает `router.push(navigate)`.
  - Успех → показать `done` и отправить `POST /buddy/feedback {turn_id, proposal_id,
    proposal_result:"accepted"}`. Ошибка → `"failed"` и текст ошибки (для 400
    `pick_an_option` — список вариантов). «Not now» → `"dismissed"`.
  - НИКОГДА не выполнять steps без клика.
- **👍/👎 под ответом Drop** → `POST /buddy/feedback {turn_id, rating:"up"|"down"}`.
- **Скрепка в чате**: тот же `uploadOriginalResume(...)`, что в `ResumeATSPanel`, затем `ask`
  с `attachment:"resume_pdf"` и текстом вроде «I uploaded a new resume». Drop сам предложит
  карточку «Rebuild».
- **Ссылка на заявку** `/dashboard/history?app=<id>`: в `HistoryView` прочитать `app` из
  `window.location.search` (как `SettingsView.tsx` 83–98), раскрыть эту карточку и
  прокрутить к ней.
- **Settings → «About you» (факты)**:
  - Список `GET /profile/facts`; правка и добавление через `POST /profile/facts`
    (`question` = короткая метка, `answer`, галочка «Mention in cover letters» = `in_letters`).
  - Удаление `DELETE /profile/facts/{id}`.
  - В History над хендбэками стоит показать `GET /personal-questions` — тот же список, что в
    попапе.
- **Админка** (Hellometrix, отдельное приложение): секция `buddy` рисуется общим рендером
  metrics/timeseries/tables. Если рендер общий, ничего делать не надо — проверить глазами.


## Ежедневный скан Drop — задачи (передать Игорю)
