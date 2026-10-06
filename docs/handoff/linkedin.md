# linkedin — лейн LinkedIn Easy Apply

Обновлено: 2026-10-05 · ветка: `linkedin/groundwork` (PR «linkedin: groundwork — per-platform cap, opt-in host, capture kit (no live behaviour)», ext 1.8.38, не смержен)

## Состояние

**Живого поведения на LinkedIn нет.** Этот PR — только каркас: кап, opt-in хост, набор для
снятия фикстур. По умолчанию расширение LinkedIn не трогает, кампания там не стартует.

### Разведка (факты до PR)

- В `content.js` лежит мёртвый v1-пайплайн: `detectPhaseLinkedIn` (~:5691),
  `phase1_linkedinList` (~:5825), `phase2_linkedinDetail` (~:5748), `phase3_linkedinForm`
  (~:5781). `phase3` никогда не жмёт Submit: шлёт `DETECTION_TRIPPED` с `linkedin_review`.
- #167 (cfbde56) убрал `*.linkedin.com` из `host_permissions` и `content_scripts.matches`
  манифеста, поэтому ничего из этого не исполняется.
- `background.js`: `buildLinkedInUrl` (~:943), `AUTO_APPLY_PLATFORMS` (~:1000) включал
  `linkedin`. Значит, старый префс с `linkedin` мог открыть кампанию на поиске LinkedIn.
- Кап на платформу был одним числом `MAX_PER_PLATFORM = 15` (`app/db/subscriptions.py`).
- Паттерн opt-in уже был: `optional_host_permissions: ["<all_urls>"]`, `pill-allow.js` и
  `pill-everywhere.js` (`chrome.permissions.request` + `chrome.scripting.registerContentScripts`).

### Что сделал PR

1. **Кап по платформам (бэкенд).** `DEFAULT_MAX_PER_PLATFORM = 15`,
   `MAX_PER_PLATFORM = {"linkedin": 5}`, читать только через `max_per_platform(platform)`.
   Читатели: `check_can_apply` (сохранение заявки), `/campaign/queue`
   (`cap_per_platform` = дефолт + новое `cap_by_platform`), `/campaign/status`
   (`limit_per_platform` = дефолт, новое `limit_by_platform`), `get_usage_summary` и
   `/stats` (`max_per_platform` = дефолт, новое `max_by_platform`). Одно число осталось
   дефолтом, поэтому расширения в сторе не видят изменений. Тесты:
   `tests/test_platform_caps.py`, `test_campaign_queue.py`, `test_ats_pipeline.py`.
   - Расширение: `campaignCaps.byPlatform` ← `limit_by_platform`. В `content.js` есть
     `platformCap(p)`: для LinkedIn фолбэк 5, даже если бэкенд не ответил. Читают его только
     LinkedIn-фазы.
   - **Зеркала на сайте (не правил, другой репо):** `components/dashboard/TapView.tsx:16`
     хардкодит `MAX_PER_PLATFORM = 15`. `components/dashboard/UsageBanner.tsx` рисует все
     платформы против одного `max_per_platform`. `lib/api.ts:131` — тип `max_per_platform`.
     Понадобится в PR, где LinkedIn появится в UI.
   - ⚠️ **Админ кап не проходит.** `check_can_apply` для `admin` пропускает всё, включая
     LinkedIn 5. Если подаёт аккаунт Игоря (админ), серверного рельса нет. Остаётся только
     `platformCap` в расширении (берёт `limit_by_platform` из `/campaign/status`, тот
     отдаётся и админу). Решить в PR подачи: применять ли кап на LinkedIn и к админу.
2. **Opt-in хост, по умолчанию OFF** (`chrome-extension/linkedin-beta.js`, `importScripts`).
   - Флаг `linkedinBeta` в `chrome.storage.local`, по умолчанию нет = OFF.
   - Скрипт регистрируется только при флаге **и** выданном `https://www.linkedin.com/*`.
     Регистрация: `hd-linkedin-beta`, только `content.js`, без `pill.js`. Причина: при
     выданном `<all_urls>` pill-everywhere уже вешает `pill.js` на LinkedIn, а вторая копия
     даёт SyntaxError.
   - Сверка: `onInstalled`/`onStartup`/`permissions.onAdded|onRemoved`/
     `storage.onChanged(linkedinBeta)`. Ушёл флаг или право → скрипт снимается.
   - Манифест не тронут, предупреждения CWS нет.
   - Переключатель — в попапе, секция `#li-dev`. Видна только у unpacked-сборки
     (`management.getSelf().installType === "development"`) или если флаг уже включён.
3. **Кампания на LinkedIn не стартует.** `CAMPAIGN_START_PLATFORMS =
   hdCampaignStartPlatforms(AUTO_APPLY_PLATFORMS)` без linkedin.
   - Из этого списка читают `pickPrimaryPlatform`, `pickAtsOpener` и `hasBoard`.
   - Выбор «только LinkedIn» (+ discovery) → отказ `linkedin_not_ready`, а не тихий
     фолбэк на Indeed.
   - В `content.js` `LINKEDIN_APPLY_ENABLED = false`: `runPhase` на LinkedIn сразу выходит.
   - `AUTO_APPLY_PLATFORMS` остался с linkedin только ради `OPEN_PLATFORM_LOGIN` (кнопка
     «connect» на сайте).
4. **Набор для снятия фикстур** (`content.js`, блок `CAPTURE KIT`).
   - Попап → «Capture this page» → `HD_LINKEDIN_CAPTURE` в активную вкладку LinkedIn →
     сериализация DOM (+ открытые shadow roots как `<template shadowrootmode>`).
   - Маска: `maskPii` (email/телефон); тела `<script>`; JSON в `<code>`; значения
     набираемых input, textarea, contenteditable; csrf-meta; `/in/<slug>`; member URN.
     Имя, почта, телефон, адрес и slug из кэша профиля → `<member>`. Плюс имя с фото «Me»,
     контейнеры «Me»/identity и фото профиля.
   - Файл скачивает попап: `linkedin-<search|view|modal-step-N|confirmation|other>-<UTC>.html`.
     Номер шага — по числу различных состояний модалки в этой вкладке.
   - Селекторы для LinkedIn-DOM (identity, модалка) **не проверены живьём**. Файл читать
     глазами до коммита.
   - Тест на синтетической странице: `tests/linkedin-capture.test.js`.
5. JS-тесты: `linkedin-beta.test.js` (регистрация ×5 состояний, манифест, старт кампании,
   инертность `runPhase`) и `linkedin-capture.test.js`. `platform-order.test.js` обновлён.

### Дыры до живой подачи (каждая — отдельный кусок работы, только на снятых фикстурах)

- **Бейдж карточки.** `phase1` ищет «Easy Apply» в `textContent` карточки (~:5854). На
  живом DOM это лист-SPAN; нужен точный селектор, иначе промахи и ложные срабатывания.
- **Кнопка Easy Apply на `/jobs/view`** (`findLinkedInEasyApplyButton` ~:5712): сверить
  с фикстурой.
- **Skip-and-continue.** Не-Easy-Apply и закрытые вакансии пропускать и идти дальше, без
  тупика (сейчас `phase2` просто `return`).
- **Филлер модалки.**
  - Гард `sponsorshipSaysYes` (#357/#358: на визу никогда не угадывать «Yes»).
  - Шаг резюме: выбрать `default_resume`.
  - Снять галку «Follow company».
- **Submit + детект «Application sent»** → `recordSubmittedApplication`. Сейчас Submit
  не жмётся вообще.
- **`handBackJob` вместо `DETECTION_TRIPPED`.** Незаполнимое возвращается человеку
  хендбэком, а не капча-стеной.
- **Тап-дека.** `TAP_APPLY_PLATFORMS` (`app/routers/jobs.py:156`) и
  `POOL_NATIVE_VERIFIED` (`background.js`) — добавлять linkedin только после живой проверки.
- **Сайт.** `CONNECTABLE_PLATFORMS` / `stage` в `jobflow-website/lib/constants.ts`
  (сейчас `stage: "connect"`, «coming soon»); бейдж и кап в UsageBanner/TapView.
- **`STATUS_MATRIX.json`** — менять только с `confirmation_id` реальной подачи.
- Детект логина LinkedIn (`detectPlatformAuth` для linkedin всегда `unknown`).

### План безопасности бана

- Сначала **только Tap**: человек одобряет каждую карточку.
- Объём: **5/день две недели**, потом **10**, потолок **15** (меняется одна строка
  `MAX_PER_PLATFORM`).
- **2–5 минут между подачами** — пейсинг в расширении. Сейчас его нет, это дыра.
- **Auto** — только для вакансий без скринера и только после ~2 чистых недель Tap.

## Следующий шаг

**Решает только Игорь:**

1. **Какой аккаунт.** Свой на крошечном объёме или согласившийся волонтёр.
2. **Риск User Agreement §8.2** (запрет автоматизации): принять осознанно.
3. **Privacy policy** должна назвать linkedin.com до любого релиза с этим хостом.
   `scripts/check_privacy_hosts.py` — в открытом PR #349; он смотрит манифест, а
   LinkedIn в манифест не попадает. Проверить руками.
4. **Одна ручная сессия снятия.**
   - Unpacked 1.8.38 → попап → «Turn on» → разрешить linkedin.com → перезагрузить вкладку.
   - Снять: поиск, `/jobs/view`, каждый шаг модалки, подтверждение.
   - Прочитать файлы и закоммитить в `chrome-extension/tests/fixtures/linkedin/`.

После фикстур: PR подачи (дыры выше), Tap-only, 5/день. Модель — Opus (`opus`).
