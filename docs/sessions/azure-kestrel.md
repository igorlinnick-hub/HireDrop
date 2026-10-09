name: azure-kestrel
session_id: 8d1f7735-e081-5adb-86da-50c25b8bc516
status: paused
lane: ai-economics
goal: Каждый вызов ИИ пишется в ai_calls (кто/зачем/модель/цена); ежедневный отчёт сам считает $/заявку и шумит на скачок; ночная предоценка пополняет запас только на потраченное (молчащим — ноль); всё в проде через PR
now: лейн передан Маку: #402 в проде; три сравнения — по слову Игоря
scope: migrations/add_ai_calls.sql,modules/ai_meter.py,app/db/ai_calls.py,scripts/ai_cost_report.py,app/routers/admin.py,app/deps.py,app/main.py,modules/ai_*.py,modules/buddy.py,modules/skills_resume.py,modules/ats_pdf_generator.py,modules/fit_queue.py(judge),app/routers/jobs.py(prejudge_pool),scripts/delete_account.py
branch: claude/ai-economics-notes
handoff: docs/handoff/ai-economics.md
started: 2026-10-09T00:53:03+00:00
updated: 2026-10-09T19:59:08+00:00
prompts: 

<!-- written by scripts/sessions.py; edit via claim/beat, not by hand -->
