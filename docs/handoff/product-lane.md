# product-lane — бэкенд, дашборд-логика, релизы

Обновлено: 2026-09-04 (вечер) · ветка: main

## Состояние

**1.7.5 на ревью CWS** (отправлен 09-04 через API, не руками). Релиз = одна команда:
`python scripts/cws_publish.py ship <zip>` (креды `CWS_*` в `.env`, настройка — `scripts/CWS_SETUP.md`).
Бэкенд зелёный: 154 теста, ruff чист, CI настоящий. Дискавери несёт инвариант
`unavailable_reason` (#130), капы не сбрасываются часовым поясом (#128), внутренние аккаунты
ограничены `ADMIN_AI_DAILY_MAX=300` (#134). Лейны разделены: сайт-витрина = сессия jobflow-07
(`jobflow-website/docs/handoff/site-lane.md`), расширение = ext-сессия, этот файл = продукт.
Правило чисел: кап/цена/лимит живут ТОЛЬКО здесь (`config.py`, `app/db/subscriptions.py`,
`app/billing_config.py`) — витрины цитируют.

## Последний заход (09-04 вечер)

**#137 смержен — профиль знает работодателя.** `current_employer` + `current_title`
прошли весь путь: `ProfileUpdate` → `_OPTIONAL_PROFILE_FIELDS` (частичный сейв их не
затирает) → `db/profile.py` (дефолты + чтение + условная запись) → миграция
`migrations/add_current_employment.sql` → правило в `form_coverage.py`. 156 тестов, ruff чист.

Замер до→после (`python scripts/form_coverage.py`, 320 форм / 3445 обязательных):
**blank 21 → 9** (0.6% → 0.3%), детерминированно 82.2% → **83.1%**, LLM-вызовов −18.
Остались 9: `gdpr disclosure` ×4, `cumulative gpa` ×3, `希望名`, `lebenslauf`.

Два хвоста висят на других лейнах — **пока их нет, фича не работает у юзера**:
1. **Миграция** — Игорь применяет руками в Supabase SQL editor. До этого колонок нет.
2. **website #115** (`feat/current-employment-settings`) — карточка в Settings, **гейт на
   миграцию**: карточка пишет НАПРЯМУЮ в Supabase, PostgREST на неизвестную колонку
   возвращает PGRST204 и валит весь запрос → сломается сохранение всей Personal Information
   (имя, телефон, адрес), не только новых полей. Порядок: миграция → мерж.
3. **ext-сессия** — ветка в `content.js` `fillTextQuestions`, ставить ПОСЛЕ ветки
   `former employee` и до фолбэка в AI:
   ```js
   } else if (/\b(current|most recent|present)\b.*\b(employer|company|job title|title|position|role)\b/i.test(label)
              && !/may we|contact|how (are|do|did)|using|why|describe|reflect|scope/i.test(label)) {
     value = /\b(job title|title|position|role)\b/.test(label)
       ? (profile.current_title || "") : (profile.current_employer || "");
     if (!value) continue; // never invent an employer — hand back instead
   }
   ```
   Гард обязателен: без него «may we contact your current employer?» и «how are you using
   AI in your current role?» получат название компании вместо ответа.

## Предыдущий заход (09-04 днём — 7 PR)

- #130 дискавери-инвариант + фикс RemoteOK; #131 манифест-чистка (16→11 хостов, v1.7.4)
- #133 имя витрины = 5 платформ (v1.7.5); #134 потолок трат внутренних аккаунтов
- #135 `cws_publish.py` — CWS-релиз командой (OAuth-клиент создан, токен в `.env`)
- #132 хендоффы получили «Следующий шаг»; уборка: 3 репо синхронизированы, ~25 веток удалено
- Замер филлера: 320 форм / 3445 вопросов → 82.2% детерминированно, 0.6% hand-back

## Сломано / не доделано

- Тир `elite` мёртв, но промо раздаёт его по умолчанию — Игорь сказал промо пока НЕ трогать.
- `/connections{,/connect,/disconnect}` мертвы в обоих клиентах — держим, пока юзеры на 1.4.4.
- Троттлинг фонового окна 5-6 мин/форма (цель <90с) — только с ре-верификацией живой подачей.
- Observability = один детектор (#125); `ALERT_EMAIL` ждёт `railway login` Игоря.

## Следующий шаг

Довести #137 до юзера (миграция Игоря → мерж website #115 → ветка в `content.js` у
ext-сессии) — до этого колонки не существует и карточка бы ломала сохранение профиля.

Дальше по величине выигрыша, уже без чужих лейнов: **`cumulative gpa` и `gdpr disclosure`**
— это 7 из оставшихся 9 blank'ов, но оба спорные (GPA часто пусто у сеньоров, GDPR —
согласие, а не факт). Честнее следующий заход потратить на **топ LLM-вопросов**: 573 вызова
всё ещё уходят на форму, и первые строки `scripts/form_coverage.py --misses 20` — это
шаблонные «have you ever been employed by X?» / «how did you first learn about X?», которые
детерминизируются той же ценой, что и employer/title.
