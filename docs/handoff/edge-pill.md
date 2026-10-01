# edge-pill — ручка у края на сайтах вакансий (Wispr-style)

Обновлено: 2026-09-30 · ветка: main

## Состояние

`chrome-extension/pill.js` — второй content script в той же записи манифеста, что и
`content.js`. Подключается на Indeed, ZR, Greenhouse, Lever и Ashby.

Ручка 12×64 у правого края. По наведению раскрывается карточка 260×124, как в макете
https://claude.ai/artifact/7VnKDd4so2bJ8Mm1qdhXsF:
- «N / cap today» и строка статуса;
- одна кнопка: Stop · Open (капча/terms, фокус на вкладку стены) · View (кап → History) ·
  Start (→ дашборд).

В `background.js` три обработчика: `PILL_TAB`, `PILL_OPEN`, `PILL_FOCUS_HANDOFF`. Чип «−»
пишет хост в `pillHiddenHosts`; попап показывает «Show again» вне `#body-main`.

- #283 → **1.8.24 отправлен в CWS** (на ревью).
- #284 → **1.8.25**, в Chrome Игоря. Стор не примет её, пока 1.8.24 на ревью;
  зип `dist/hiredrop-ext-1.8.25.zip` собран.
- **1.8.26: полоска на всех сайтах по согласию** (PR ext/pill-everywhere). В манифесте
  `optional_host_permissions: ["<all_urls>"]`. В попапе строка «Edge pill on job sites only ·
  Show it on every site» → `chrome.permissions.request`. Регистрацию делает background по
  `permissions.onAdded`, а не попап: попап закрывается, когда Chrome показывает запрос.
  Логика в `pill-everywhere.js` (`importScripts`). Исключено всё, что уже покрыто
  статическими `content_scripts`; список берётся из манифеста. Уже открытые вкладки получают
  полоску через `executeScript`. Сверка регистрации идёт в `onInstalled`/`onStartup`/
  `onAdded`/`onRemoved`. Зип `dist/hiredrop-ext-1.8.27.zip` собран из всей папки.
- **1.8.27:** у карточки «N to finish» в попапе были цвета ночной темы (#215 вышел через
  день после дневного попапа #200), поэтому название читалось белым по кремовому. Цвета
  переведены на токены попапа. Пустое название заменяется на «Indeed application».
- Проверено живьём настоящим расширением в изолированном Chromium на Indeed, Lever и
  Greenhouse, светлая и тёмная тема: наведение, Start, «−», reload, «Show again».
  **В бою, во время кампании, НЕ проверено.**

## Правила (каждое — защита прогона)

- **Не монтируется в табе/окне кампании.** CDP-ховеры там настоящие и открыли бы
  карточку под кликом. Решает `hdPillHidden`.
- **Закрытый shadow root на `<div>` под `<html>`.** Не в `body.innerText`, не находится
  `querySelector`'ами `content.js`.
- **Без сети и без `web_accessible_resources`.** Страница не может зондировать
  `chrome-extension://`.
- **Числа из `chrome.storage.local`** (те же ключи, что `GET_STATUS`). `/stats` на
  навигацию не зовётся.
- **Start не стартует, а открывает дашборд.** Гейты запуска живут там.
- **Имена верхнего уровня только с префиксом `hdPill`/`HD_PILL`.** Мир общий с
  `content.js`, совпадение имён = SyntaxError при загрузке. Проверено vm-пробой с
  негативным контролем.

## Грабли

- **Зип для стора собирать из ВСЕЙ папки:** `cd chrome-extension && zip -qr
  ../../dist/hiredrop-ext-X.zip . -x 'tests/*' -x '*.DS_Store'`. Зип 1.8.25 собран по явному
  списку файлов. По такому списку из 1.8.26 выпадет `pill-everywhere.js`, `importScripts`
  упадёт и SW умрёт у всех юзеров стора. Проверка: распаковать зип и загрузить в Chromium.
- **Диалог разрешения Chrome в headless не нажать.** Живая проба идёт на копии, где
  `<all_urls>` переложен в `host_permissions`. Сам путь «клик → запрос → onAdded» проверяется
  только в настоящем Chrome руками.
- После отзыва доступа полоска во вкладках, уже открытых, живёт до перезагрузки страницы.

- **job-boards.greenhouse.io подменяет сам `<html>` ~через 1 с после загрузки.** Хост
  возвращает MutationObserver на `document` + текущий `<html>`, не больше 20 раз.
  Наблюдатель только на `<html>` НЕ работает: элемент новый.
- Живая проба: `launch_persistent_context(channel="chromium", headless=True,
  args=[--load-extension=…])`; хост = ребёнок `<html>`, чей style содержит `2147483646`.
  Indeed headless отдаёт «Blocked», но полоска там тоже есть.

## Следующий шаг

1. Живьём в Chrome Игоря, после «давай» и только когда не идёт прогон ext-сессии: синк →
   OFF/ON → в попапе «Show it on every site» → «Разрешить» → полоска на gmail/любом сайте
   без перезагрузки → «Job sites only» → после перезагрузки страницы полоски нет.
2. Когда стор одобрит 1.8.24: `cws_publish.py ship dist/hiredrop-ext-1.8.27.zip`. В
   витрине (Privacy) дописать обоснование optional `<all_urls>`: «показать дневной счётчик
   по желанию юзера; страница не читается». Широкий доступ может удлинить ревью.
