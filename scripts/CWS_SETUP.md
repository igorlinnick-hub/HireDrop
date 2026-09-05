# Публикация в Chrome Web Store из терминала — разовая настройка

`scripts/cws_publish.py` заливает и отправляет расширение на ревью одной командой.
Чтобы он заработал, Google должен один раз выдать нам доступ. 5 минут, делается однажды.

## 1. Проект и API (2 мин)

1. [console.cloud.google.com](https://console.cloud.google.com/) → выбрать проект или создать новый
   (имя любое, например `hiredrop-release`).
2. Слева **APIs & Services → Library** → найти **Chrome Web Store API** → **Enable**.

## 2. Согласие и клиент (2 мин)

3. **APIs & Services → OAuth consent screen** → тип **External** → заполнить имя приложения
   и свой email → сохранить. В разделе **Test users** добавить свой Google-аккаунт
   (тот, под которым издатель расширения). Публиковать consent screen НЕ надо.
4. **APIs & Services → Credentials** → **Create credentials → OAuth client ID** →
   тип приложения **Desktop app** → создать. Скопировать **Client ID** и **Client secret**.

## 3. Ключи и токен (1 мин)

5. Дописать в `jobflow/.env`:

   ```
   CWS_CLIENT_ID=<client id>
   CWS_CLIENT_SECRET=<client secret>
   ```

6. Выполнить:

   ```bash
   python scripts/cws_publish.py auth
   ```

   Скрипт напечатает ссылку → открыть → разрешить → Google покажет код → вставить в терминал.
   В ответ он выдаст строку `CWS_REFRESH_TOKEN=...` — дописать её в тот же `.env`.

Токен долгоживущий: этот шаг больше не повторяется.

## Дальше — релиз одной командой

```bash
python scripts/cws_publish.py status                     # что стор думает сейчас
python scripts/cws_publish.py ship ../dist/hiredrop-ext-1.7.5.zip   # залить + отправить на ревью
```

Отдельно, если нужно: `upload <zip>` (только в черновик) и `publish` (только отправить).
`publish trustedTesters` — выкатка на тестеров вместо всех.

## Чего этот путь НЕ делает

API управляет **пакетом**, но не витриной. Через него нельзя:

- переключить видимость **Публичное ↔ Для тех, у кого есть ссылка**;
- поменять развёрнутое описание, скриншоты, промо-тайлы, категорию.

Это осталось в Developer Dashboard. Заголовок и подзаголовок витрины — исключение:
они берутся из `manifest.json`, значит едут вместе с пакетом и правятся кодом.

## Если что-то пошло не так

- `HTTP 401` — истёк или отозван refresh token: повторить шаг 6.
- `HTTP 403` с упоминанием API — не включён Chrome Web Store API (шаг 2).
- `uploadState=FAILURE` — стор отверг пакет; скрипт печатает `itemError` с причиной,
  чаще всего это версия не выше опубликованной или проблема в манифесте.
