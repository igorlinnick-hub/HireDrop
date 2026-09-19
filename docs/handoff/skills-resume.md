# skills-resume — второе резюме + диал дефолта

Обновлено: 2026-09-19 · ветка: skills-describe (#199/#163 смержены; #202/#166 в полёте)

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

**Описание скилов (09-19, #202/#166):** `profiles.skills_description` — свободный текст
своими словами, миграция применена. `POST /profile/skills/describe` сохраняет (trim,
кап 4000); generate принимает `description`, сохраняет его ПЕРВЫМ и падает на
сохранённый, если не передан → слова переживают регенерацию. В UI: модалка
«Describe your skills» (Save only / Save & Generate) + шаг «List your skills»
в чеклисте (done, если есть skill_groups ИЛИ skills_description).

## Последний заход
- Добавлены skills_description (миграция применена и проверена REST), endpoint
  describe, подмешивание описания в структурирование, тесты trim/cap/clear.
- UI: модалка описания, рендер skill_groups под блоком, шаг скилов в чеклисте.
- Полный pytest зелёный (exit 0), ruff чисто, next build чисто.

## Сломано / не доделано
- **Живой генерации ещё не было ни разу** — кнопку Generate Skills Version никто не
  нажимал на настоящем резюме; структурный промпт остаётся гипотезой до первого PDF.
- skill_groups показываются, но не редактируются вручную (только регенерацией).
- Дефолт-диал в проде, но подача с `default_resume='skills'` живьём не наблюдалась.

## Следующий шаг
Прогнать живую генерацию на резюме Игоря (Settings → Resume & ATS → Generate Skills
Version) и посмотреть PDF глазами.
