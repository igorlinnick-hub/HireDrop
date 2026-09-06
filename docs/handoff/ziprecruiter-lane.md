# HANDOFF — ZipRecruiter / движок подачи

Обновлено: **2026-09-06** · ext **1.7.5** (репо и Рабочий стол синхронны) · всё в main

## ЛЕЙН ЗАКРЫТ: запись подачи на ZR снята 09-06 21:20 UTC

Цель лейна («подача С ЗАПИСЬЮ именно на ZipRecruiter») достигнута, обе улики:
- лог: `✅ Applied: Social Media Manager @ King Jesus International Ministry` (21:20:40);
- БД: applications row `status=applied`, platform=ziprecruiter.

Прогон: native auto walk (Miami, «social media manager»+), Quick Apply, контактный шаг
прошёл на засеянном адресе (Honolulu 96816 — засев из резюме/ручной, PR #152). Вторая
форма (WMX) пошла следом: STEP 1 заполнил phone+9 combo+7 text. Кампанию гонял
jobflow-0b через postMessage-мост (UI-кнопка Start сглючила под osascript — движковый
путь чист). Улики в матрице: `STATUS_MATRIX.json` → ziprecruiter (verified_on 09-06).

## Что помнить по ZR (выжимка закрытого лейна)

- У ZR НЕТ кнопки Submit — последний шаг тоже «Continue» (исторический корень потери записей).
- Выдача узкого ключевика кончается за ~5 страниц → external-apply; брать широкие.
- Пустые страницы: две подряд закрывают ключевик; авто-переключение платформ работает (#104).
- ZR серверу не скрейпится (CF 403 «forbidden aa») — только in-browser; SERVER_SCRAPE_SKIP честен.
- Капчу не решаем: пауза на человека. CF passive interstitial движок переживает сам.
- Полная история 19 багов пути подачи — в git-истории этого файла (`git log -p`).

## Следующий шаг

Ничего активного. Лейн закрыт. Реоткрывать только при регрессии живой подачи ZR
(сигнал: ops-watch/stall-watch или падение записей в applications по platform=ziprecruiter).
