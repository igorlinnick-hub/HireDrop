# skills-resume — второе резюме + диал дефолта

Обновлено: 2026-09-18 · ветка: skills-resume (backend PR #199, website PR #163)

## Состояние
Гибридный skills-first формат (НЕ функциональное резюме — отвергнуто: рекрутеры/ATS
читают его как сокрытие пробелов): сгруппированные скилы ведут страницу, каждая
работа = 1–2 строки + «skills gained». `modules/skills_resume.py` реиспользует
хелперы `ats_pdf_generator` (один Claude-вызов структурирует → PDF+DOCX).
Endpoints: `POST /profile/resume/skills/generate` (тот же атомарный AI-квота-клейм,
что у ats/generate), `GET .../skills/url|docx-url`, `POST /profile/resume/default`.
Миграция `add_skills_resume.sql` ПРИМЕНЕНА к проду и проверена через PostgREST
(skills_resume_url, skill_groups jsonb, default_resume + CHECK).

**Диал — одна власть:** `profiles.default_resume` ('original'|'ats'|'skills');
`ats_approved` — только legacy-fallback при NULL (урок fit-mode: две власти = баг).
`best_signed_url` в apply-пути: per-job tailored → диал → legacy → оригинал;
пропавший сгенерированный файл падает на оригинал, не 404-ит подачу.
Website: ResumeATSPanel — блок Skills-First Version + трёхпозиционный дефолт,
все «use X» пишут диал + best-effort sync legacy approve/decline.

## Последний заход
- Создан модуль, endpoints, миграция (применена), тесты `test_skills_resume.py`
  (порядок секций, компактность, матрица диала с monkeypatch storage).
- ResumeATSPanel переведён с бинарного atsApproved на effectiveDefault.
- Полный pytest зелёный, ruff чисто, next build чисто.

## Сломано / не доделано
- Q&A «опиши скилы» для генерации НЕ подключён в UI (бэкенд принимает answers;
  можно переиспользовать `/profile/ats/questions`-паттерн или свой промпт).
- Живой прогон генерации на реальном резюме не делался (структурный промпт —
  гипотеза до первого вызова).
- Чеклист активации ещё не знает про скилы (шаг «перечисли скилы» — след. итерация).
- skill_groups сохраняются, но нигде не показываются/не редактируются.

## Следующий шаг
Смержить #199 → #163 (в этом порядке), затем прогнать живую генерацию на резюме
Игоря и посмотреть PDF глазами.
