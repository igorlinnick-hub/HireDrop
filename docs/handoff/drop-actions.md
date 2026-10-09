# drop-actions — Drop с кнопками, память ответов о себе, наблюдение за Drop

Сессия: teal-fox · лейн drop-actions · Обновлено: 2026-10-09 · ветка: main · ждёт: Drop-уточнитель (новая задача) + недельный замер

## Состояние

- **Всё в проде**: бэкенд #401 (миграция `personal_facts` применена), сайт web #325 (карточки Drop,
  👍/👎, скрепка, `?app=`, «Only you can answer», «About you») и web #328 («Change» под каждым
  ответом в History «What we answered for you» → `POST /profile/facts`, source `history`).
- **Решение Игоря 10-09**: на логистику (переезд/офис/выходные/командировки) отвечаем «Yes» без
  вопросов (#410, лейн drop-finish), человек правит в History кнопкой Change, правка запоминается и
  идёт во все следующие формы раньше дефолта и модели. Факты о себе («живёте рядом с X?», дата
  выхода, виза, аттестации) по-прежнему уходят человеку.
- **Расширение**: 1.8.45 (3b06dea) у Игоря в Chrome (подтвердила plum-salmon), в CWS на ревью с 10-09.
  Следующий релиз 1.8.46 (п.1+п.2 drop-finish: логистика Yes + снимок формы) делает calm-quail;
  до него `form_answers` пустые и кнопка Change не видна.
- **Живая проверка 10-09 под `+buyer1`**: факт сохраняется → тот же вопрос отвечается из него
  (`from_user`) → удаляется; Drop с `cards:true` на «покажи заявку Linear» даёт карточку
  `open_application` с `navigate=/dashboard/history?app=<id>`; feedback пишется. Тестовые строки удалены.
- **Наблюдение**: шаг «3в. Drop вчера» в prod-sweep; разовый разбор недели — launchd
  `com.igor.drop-week-review` 2026-10-16 09:25 (`.claude/scheduled/drop-week-review.sh` в корне,
  отчёт → `docs/reviews/2026-10-16-drop-week.md` корня + уведомление, задача удаляет себя).

## Последний заход

- teal-fox (10-09): миграция → ревью стыков → мерж #401 → sync + CWS → prod-sweep → web #325 →
  живая проверка → web #328 (Change), согласовано с calm-quail (AnswersBlock мой, ряды хендбэков её).

## Сломано

- Мелочь: Drop называет `date_applied` (дата без времени) как полночь UTC в поясе юзера —
  заявка от 10-09 звучит «Oct 8, 2:00 PM» на Гавайях. Где-то в `modules/buddy_facts.py`.
- Вопрос длиннее 200 символов: снимок формы режет `q` до 200, `personal_facts.match` сравнивает
  целиком → правка такого вопроса из History не совпадёт со следующей формой. Редко.
- Риски с #401 в силе: факты одним jsonb (≤40), `openPopup()` — Chrome 127+.

## Следующий шаг

**Новая задача лейна (передала plum-salmon 10-09, со слов «Игорь одобрил»; подтвердить у Игоря):
Drop-уточнитель.** Drop изредка спрашивает про вакансию 👍/👎 или 0–10, юзер отвечает или
пропускает. Правила: ≤1–2 вопроса в день; только пограничные вакансии (оценка судьи у планки
юзера или вакансия, которую мы отсекли); вопросы по шаблонам, без свободного текста модели;
пропуск ничего не стоит; одну категорию повторно не спрашивать. Ответы → поправка планки юзера +
замер точности спорной зоны судьи (40–70, Sonnet) — вход для ai-economics. Контракт:
`docs/handoff/keyword-yield.md` «Контракты» (PR #416, на 10-09 НЕ слит — сначала дождаться мержа).
Модель: **Opus**. Начать с `how` по `modules/buddy_actions.py` (шаблонные карточки) и fit-судье.

Недельный замер: 10-16 прочитать отчёт launchd (`docs/reviews/2026-10-16-drop-week.md`),
по нему решить: чинить ли дату в Drop, сколько правок через Change, вышла ли 1.8.45/1.8.46 в CWS.

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
