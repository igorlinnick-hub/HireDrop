# drop-actions — Drop с кнопками, память ответов о себе, наблюдение за Drop

Сессия: teal-fox · лейн drop-actions · Обновлено: 2026-10-09 · ветка: main (бэкенд слит), сайт — `feat/drop-cards` в hiredrop-website

## Состояние

- **Бэкенд в проде** (PR #401, `3b06dea`): факты о человеке (`modules/personal_facts.py`,
  `profiles.personal_facts`), ответчик сперва смотрит в память и на вопросы-обстоятельства
  (переезд, где живёшь, офис, командировки, смены, дата выхода) отвечает только из фактов,
  иначе `ask_person`; письмо знает город вакансии; эндпоинты `/personal-questions`,
  `/profile/facts` (GET/POST/DELETE), `/profile/ats/contact`, `/buddy/feedback`; Drop с
  `propose_action` (карточки, путь задаёт сервер) — включается только флагом `cards: true` с сайта.
- **Миграция** `add_personal_facts.sql` применена 10-09, колонка читается из прода.
- **Расширение 1.8.45**: на Рабочем столе (sync-ext), в CWS отправлено на ревью 10-09
  (`dist/hiredrop-ext-1.8.45.zip`). Пока стор раздаёт 1.8.44: радио/селекты на «relocate?»
  по-старому жмут Yes; обязательные текстовые вопросы-обстоятельства («when can you start?»)
  уходят в хендбэк — отвечать в History, ответ запоминается.
- **Наблюдение**: каждый ход Drop → `activity_log` (phase `buddy`); `scripts/buddy_review.py`;
  ежедневный шаг «3в. Drop вчера» в prod-sweep (корневой `.claude/skills/prod-sweep/SKILL.md`,
  коммит `00d5e0e`), пробный прогон 10-09: 0 вопросов, чисто.
- **Сайт в проде** (web #325, `df63f92`): карточки Drop (`cards: true`), 👍/👎, скрепка PDF,
  `/dashboard/history?app=<id>`, «Only you can answer» в History, «About you» в Settings.
  Живой карточки Drop end-to-end ещё не было.

## Последний заход

- teal-fox (10-09): миграция → ревью-агент по стыкам (старое ext против нового бэкенда, IDOR,
  кэш, таймауты content.js — всё SAFE) → полный прогон в worktree (pytest 74%, ruff, ratchet,
  ext 57/57) → мерж #401 → Railway раскатил → sync-ext + CWS ship → шаг в prod-sweep.
  Запущен агент на сайт.

## Сломано

- Ничего не сломано. Риски: Игорь ещё не делал OFF/ON 1.8.45 (проверить версию в попапе);
  `/buddy/feedback` принимает любой `turn_id` (пишет под своим user_id, но джойн голосов в
  админке `buddy_log.py:183` не по user — косметика); факты одним jsonb (≤40), два окна
  одновременно могут потерять ответ; `openPopup()` из уведомления — Chrome 127+.
- В основной копии hiredrop-website лежат чужие незакоммиченные правки чата (`BuddyPanel.tsx`,
  `Buddy.tsx`, `DropFigure.tsx`) — при мерже `feat/drop-cards` возможен конфликт.

## Следующий шаг

Модель: **Opus**. Живая проверка под `+buyer1`: спросить Drop про заявку → карточка Open → History
раскрыта; добавить факт в About you. Через неделю (~10-16): `buddy_review.py --days 7`,
`measure_handback_share.py --days 7`, `handback_reasons.py --days 7` — ждём рост хендбэков
на переезд/офис/смены первые дни, потом спад. Проверить `cws_publish.py status` (1.8.45 опубликована?).

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
