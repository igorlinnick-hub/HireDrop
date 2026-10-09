# sessions-board — стандарт доски сессий (авто-запись, авто-пульс, stats)

Сессия: lunar-bison · лейн indeed-prejudge (эта работа — параллельно, по просьбе Игоря 10-08)

Обновлено: 2026-10-09 · ветка: feat/sessions-auto (запушена, PR ещё НЕ открыт)

## Состояние
- На ветке (`dd9049e`, `d1e115d`, worktree `../.wt-sessions-auto`): SessionStart кладёт каждую сессию на
  доску (`open`, файл в gitignored `docs/sessions/.open/`), UserPromptSubmit `hook-prompt` — цель «авто: …»
  из первого запроса (скраб секретов, обрезка на словах password/токен/ключ…), регистрация пропущенных
  хуком старта, одно напоминание на 3-м запросе; Stop `hook-beat` — пульс раз в минуту (paused не будит);
  `done` возвращает сессию в `open`; смена лейна паркует старый; имена без коллизий; короткий `--session`
  хранит полный uuid. `stats` — 4 цели (видимость 100% против транскриптов `~/.claude/projects`, лейн ≥80%
  для 3+ запросов, хендофф 100%, тихие смерти 0). Лог `docs/sessions/.events.jsonl` (gitignored).
- 1499 pytest, ruff чисто, работает на /usr/bin/python3 3.9, хук ~40 мс. code-review: 15 находок, все закрыты.
- База «до» (10-08 вечер, по транскриптам): из 7 работавших сессий на доске 4 (57%).

## Последний заход
- Причина невидимости «Экономики ИИ»: стартовала в 12:17:13, хук доски вписан в 12:17:20.
- `jobflow/.claude/settings.json` на ветке уже содержит UserPromptSubmit/Stop с `2>/dev/null || true`.

## Сломано / не доделано
- PR не открыт, не смержено. Корневой `~/Code/JobFlow/.claude/settings.json` (не закоммичен, чужие правки
  там же) ещё НЕ содержит `hook-prompt`/`hook-beat` — добавить ТОЛЬКО после мержа + `git pull --ff-only`
  в `jobflow/` (иначе argparse exit 2 блокирует каждый запрос). Образец — `docs/sessions/README.md`.
- Сессии, открытые внутри worktree, пишут свою доску (ограничение дизайна, не чинилось).

## Следующий шаг
По «давай мерж» Игоря: `gh pr create` из `feat/sessions-auto` → мерж (не во время прогона: пуш в main = редеплой
Railway) → `git -C jobflow pull --ff-only` → добавить UserPromptSubmit+Stop (`hook-prompt`/`hook-beat`, с `|| true`)
в корневой `.claude/settings.json` → открыть новую сессию и проверить, что она на доске; `sessions.py stats` через день.
