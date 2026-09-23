# night-shift — серверный исполнитель ATS-подач (пока юзер спит)

Обновлено: 2026-09-23. Лейн стартован по прямому «вперед» Игоря. Тест — на его аккаунте
(a0775013, «тестируй с моего аккаунта уже как есть»). Cover letters / resume признаны готовыми.

## Решённая архитектура (разведка 09-23, read-only)

- **Greenhouse**: reCAPTCHA **Enterprise, score-режим** — `grecaptcha.enterprise.execute(key,
  {action:"apply_to_job"})`, endpoint `recaptcha.net/recaptcha/enterprise.js?render=<key>`.
  Челленджа человеку НЕТ: браузер получает токен сам, Greenhouse оценивает score.
  `captchaFailed`/`captcha_retried` в бандле = обработка низкого score.
- Сабмит-payload GH: `job_application` + `csrfToken` + `fingerprint` + `jobApplicationRequestToken`
  + recaptcha token → **чистый HTTP-POST хрупок и палевен. Основной путь = headless Playwright**:
  реальная страница, реальный JS, всё собирается само.
- **Ashby**: на живой странице 2 reCAPTCHA site-key (universal) — считать аналогом; GraphQL
  интроспекция закрыта. Второй в очереди после GH.
- **Lever — не берём** (interactive captcha per-company). Indeed/ZR — не берём (403/CF, бан-сигнатура).
- Капча-солвер-сервис = **fallback**, не основа: нужен только если score режет. Первый рычаг —
  нормальный браузер-отпечаток + жилой IP (тест с Mac Игоря = жилой IP; Railway-датацентр — риск
  низкого score → резидентный прокси, решение после замера).

## Фазы

- **P0 разведка** ✅ (выше).
- **P1 MVP-исполнитель** (`scripts/night_shift/executor.py`, Python+Playwright, НЕ деплоится пока):
  один GH-job из пула юзера → страница → заполнение (профиль + resume PDF + обязательные вопросы
  через modules/ai_question_answer + письмо через ai_cover_letter) → **dry-run: скриншот, БЕЗ сабмита**.
  Гейт качества: скриншот смотрится глазами до любого live.
- **P2 live-сабмит**: тот же путь + клик Submit + подтверждение (confirmation-страница) →
  запись в `applications` + `activity_log` + дедуп по link (mark_applied_by_link) + кап бэкенда.
- **P3 очередь**: обход пула (status=new, greenhouse/ashby, фильтр юзера как на показе — pool-is-an-archive),
  fit-гейт СЕРВЕРНЫЙ до сабмита, кулдауны, лимиты (кап 30 общий с ext — одна власть в бэкенде).
- **P4 ночной триггер**: только auto-юзеры, heartbeat мёртв (ext молчит) → сервер подхватывает;
  проснулся → отдаёт. Крон/воркер на Railway + решение про прокси по замеру score.

## Грабли, уже известные

- fit_score нигде не хранится; jobs.score 0–10 ≠ fit 0–100. Серверному фильтру звать ai_fit_judge.
- Пул-ссылки Ashby в БД с задвоенным `/application/application` → 404; базовый URL живой.
- Пул = архив: фильтры юзера применять при выборе (см. память pool-is-an-archive).
- 8 AI-путей читают резюме через resume_text_for — исполнителю тоже (не оригинал мимо диала).
- Замер по НУЖНОМУ user_id и от started_at (грабли 09-22).
