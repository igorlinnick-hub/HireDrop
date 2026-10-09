name: hazel-tapir
session_id: 7fe19b12-d78b-5965-8ac1-f5614503df30
status: paused
lane: ai-economics
goal: Каждый вызов ИИ пишется в ai_calls (кто/зачем/модель/цена); ежедневный отчёт сам считает $/заявку и шумит на скачок; ночная предоценка пополняет запас только на потраченное (молчащим — ноль); всё в проде через PR
now: PR #400 CI зелёный → мерж; дальше миграция с Мака
scope: migrations/add_ai_calls.sql,modules/ai_meter.py,app/db/ai_calls.py,scripts/ai_cost_report.py,app/routers/admin.py,app/deps.py,app/main.py,modules/ai_*.py,modules/buddy.py,modules/skills_resume.py,modules/ats_pdf_generator.py,modules/fit_queue.py(judge),app/routers/jobs.py(prejudge_pool),scripts/delete_account.py
branch: claude/blissful-knuth-e3z2bp
handoff: docs/handoff/ai-economics.md
started: 2026-10-08T22:58:26+00:00
updated: 2026-10-09T00:26:31+00:00

<!-- written by scripts/sessions.py; edit via claim/beat, not by hand -->
