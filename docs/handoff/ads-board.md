# Ads board (платная реклама: пиксель, CAPI, траты, кампании, команда агентов)

Обновлено: 2026-10-05 (поздняя ночь) · ветка: ads-mcp-spend → main

## Состояние

- **Meta**: портфель Hiredrop 2022625988457757, аккаунт HireDrop Ads 1672047784439131 (USD, Visa), страница
  1430549256789872, пиксель 1113284891046203 + CAPI живые (Railway `META_PIXEL_ID`/`META_CAPI_TOKEN`).
  **Лимит трат аккаунта $30** (Игорь поднимет до $300).
- **Управление Meta = официальный MCP `meta-ads`** (OAuth Игоря 10-05, работает). Кампания
  `HD · Meta · R1 · Sign-ups` 120254913703970368 ($6/день, выставлено 10-05) + ad set 120254913704300368 —
  оба PAUSED. **Объявлений 0** (M1 с лицами создан и удалён по слову Игоря вместе с 2 креативами).
- **Креативы ОДОБРЕНЫ Игорем 10-05**: `content-lab/ads/creatives/R2-drop/` — Drop без розовых щёк (фикс сходства
  с маскотом Muse/Jolly от Meta, память `project_drop_vs_muse`), 3 угла × 4:5/9:16, `render.py`, `contact-sheet.png`
  (полосы на листе = safe-zone сторис, в PNG их нет). Спека `content-lab/ads/campaigns/meta-r1.json` уже на R2-drop.
- **Бюджет (Игорь 10-05)**: $300/мес из кармана, потом из прибыли (выручка − 30% рефералам). Кампания $6/день +
  бусты видео ~$120/мес ($15 на пост, 3 дня). Видео для бустов ещё не готовы.
- **Команда агентов**: скил `.claude/skills/ads-manager/SKILL.md` — Аналитик/Оператор ежедневно (вердикт
  `app/ads/verdict.py`, сам ставит проигравших на паузу), Креатор (пн, чемпион/претендент), Бустер (видео IG).
  Деньги/включение — только Игорь. launchd `com.igor.ads-manager` НЕ установлен — после старта кампании.

## Последний заход

- 10-05: MCP авторизован; доказано, что `ads_create_ad` с инлайн-креативом = `scripts/meta_ads.py:creative_payload()`
  (asset_feed_spec 4:5/9:16 + `url_tags` с `{{ad.id}}`) проходит без dev-mode стены.
- Грабли: на аккаунте НЕ раскатаны `ads_creative_upload_local_image` и чтение черновиков; загрузка по URL требует
  публичной ссылки, а выкладку файлов в публичный репо режет классификатор — не повторять → PNG кладёт Игорь в
  медиатеку. Щёки на сценах: `nano-banana` стирает лицо на desk-сцене, `flux-kontext-pro` справился; ручной
  inpaint OpenCV мажет очки. Метку ИИ (`self_ai_disclosure`) Игорь решил не ставить.
- Трогал: `content-lab/ads/campaigns/meta-r1.{json,state.json}`, `content-lab/ads/creatives/R2-drop/*`,
  `.claude/skills/ads-manager/SKILL.md`, память (`project_drop_vs_muse`, `feedback_ads_prep_not_creatives`,
  `project_meta_ads_mcp_route`, `project_ads_launch_decision`). Ничего не закоммичено.

## Сломано / не доделано

- **Траты Meta = агент через MCP (вариант б, сделано 10-05)**: токен `META_ADS_TOKEN` мёртв (права слетели у
  app HelloMetrics — не трогаем). `scripts/ads_spend.py ingest-mcp --date D <файл>` пишет один день ответа
  `ads_get_ad_entities` (level=ad, since=until=D) в `ad_spend` с `source=meta_mcp` через тот же
  `meta_spend.to_rows`. Доска: последняя строка Meta от агента → Graph не дёргается, статус
  «connected (ads-manager agent, Meta MCP)»; > 26 ч без записи → названо на доске. Шаг 1.3 скила `ads-manager`.
  Доказано: живой ответ MCP за 10-04 (пусто, показов не было) → скрипт → прод-Supabase, 0 строк, exit 0.
  **Не видели**: форму `amount_spent` при реальной трате (MCP отдаёт метрики только при показах) — парсер
  берёт число/строку/`$1,012.30`/`{amount,currency}`, иначе `refused`, не $0. Первый день с показами =
  проверить `status` глазами.
- `ads_get_ig_accounts` = [] — IG `@hiredrop.io` привязан к портфелю, но не к рекламному аккаунту (Connected assets)
  или MCP авторизован без IG. Без этого нет показов от IG и не работает буст.
- Railway `ADS_MONTHLY_BUDGET_USD` не задан → доска считает темп от $500 (`admin.py:79`); выставить 300.
- Google Ads не начат.

## Следующий шаг

Модель: **Opus**. Ждём от Игоря: 6 PNG `R2-drop` в Media library, IG → Connected assets, лимит $30→$300.
Затем: `ads_get_ig_accounts` → `ads_get_ad_images` по имени → хэши в `meta-r1.state.json` → 3 × `ads_create_ad`
(payload сборщика, + `instagram_user_id` если IG виден), PAUSED → ссылка на Ads Manager, включает Игорь →
установить launchd `ads-manager` (ручной `claude -p` прогон сначала) → `ADS_MONTHLY_BUDGET_USD=300` на Railway.
