# Affiliate (аффилиатка)

Обновлено: 2026-10-01 · ветка: main (несколько мелких PR за заход, все смержены)

## Состояние

**Живой платёж проверен целиком, на настоящих деньгах.** Тестовый партнёр (код `igor`,
аккаунт `hacker987602+aff1@gmail.com`) → чистый браузер → `?ref=igor` → регистрация нового
покупателя (`+buyer1`) → оплата $12 картой Игоря → `invoice.paid` → начислено $3.60 (30%) →
рефанд $12 → комиссия ушла в `reversed`. Подписка отменена сразу (`prorate=false,
invoice_now=false`), повторных списаний не будет. Инструмент: `scripts/affiliate_live_test.py
verify igor` — гоняет всю цепочку и называет первое порванное звено.

**Вход теперь account-first.** Одна кнопка на лендинге → регистрация → анкета внутри
аккаунта → «на рассмотрении» → админ жмёт Approve → письмо со ссылкой уходит само.
Публичная форма `/affiliate/apply` стала редиректом в signup (адрес напечатан на карточке,
оставлен живым). `lib/gate/landing.ts` (website) — ЕДИНОЕ правило «куда вести после входа»
для логина/письма-подтверждения/оболочки дашборда; раньше три места решали порознь и
расходились (аффилиат по паролю попадал в квиз соискателя).

**Почта юзерам не доходила вообще** — не только аффилиатская. `RESEND_FROM_EMAIL` на
Railway был пуст → Resend слал с тестовой песочницы, которая доставляет ТОЛЬКО владельцу
аккаунта Resend (Игорю). Сброс пароля не работал ни у одного реального юзера. Починено:
`RESEND_FROM_EMAIL=HireDrop <support@hiredrop.io>` (домен verified), проверено живьём на
чужой ящик. См. память `no-user-emails` — уточнена, решение 09-07 было про НЕПРОШЕНЫЕ
письма, не про все.

**Борд (Hellometrix `/hiredrop`, вкладка Affiliates):** результат Approve/Reject раньше
исчезал при обновлении списка — Игорь не успел скопировать ссылку. Теперь результат живёт
в инбоксе, а не в карточке заявки, и остаётся до нажатия Done. Таблица партнёров получила
колонку Link с кнопкой копирования — ссылку видно и после закрытия результата.

**Stripe Connect — в процессе, дальше только руками Игоря.** Мастер платформы пройден
(funds flow = platform / separate charges+transfers, industry = Other, onboarding = Stripe
hosted, dashboard = Express). Осталось два шага в самом Stripe: верификация документа
личности (форма — cross-origin iframe, скриптом недоступна принципиально) и «Confirm final
details». Без них `stripe.Account.create(type=express, ...)` на живом ключе отказывает:
"You must complete your platform profile". Фото прав (`IMG_3117/3118.JPG`,
`~/Documents/Документы США/`) найдены и названы Игорю, сама загрузка — его действие в
открытом окне Safari (window id в `$SP/stripe-wid`, см. скрипты сессии).

## Последний заход

- `HireDrop#245/#247/#276` + `website#204/#205/#213` — email при одобрении, привязка почты
  к аккаунту на сервере (не читожно из body), account-first вход, `lib/gate/landing.ts`.
- `HireDrop#278` + `Hellometrix#17` — партнёрский линк в леджере, результат Approve не
  исчезает.
- `RESEND_FROM_EMAIL` задан на Railway (Connect не в git, переменная окружения).
- Прогнан живой платёж (см. выше), подписка отменена, рефанд сделан.
- Мастер Stripe Connect Platform Profile пройден до шага identity verification.

## Сломано / не доделано

- **Stripe Connect ВКЛЮЧЁН 10-01** (identity verified + Confirm final details пройдены Игорем).
  Проверено: `stripe.Account.create(type="express", country="US", capabilities={"transfers":
  {"requested": True}})` на живом ключе создаёт аккаунт (создан и удалён `acct_1ULuLJ…`).
  Конфигурация платформы зафиксирована Stripe и НЕ меняется: buyers purchase from platform,
  separate charges & transfers, Stripe-hosted onboarding, Express Dashboard.
- **Автовыплат ещё нет** — кода нет, только ручной `affiliate_admin.py payout`.
- **PayPal-поля у партнёра в UI нет** — отпадёт с Connect (Stripe сам собирает реквизиты).
- Тестовый партнёр занял код `igor` (аккаунт `hacker987602+aff1`).

## Следующий шаг

Строить автовыплаты на Connect (модель: Opus). Миграция: `affiliates.stripe_account_id`,
`payouts.stripe_transfer_id`. Бэкенд: `POST /affiliate/payouts/connect` → Account(express) +
AccountLink → вебхук `account.updated` (payouts_enabled); ежедневная задача — `accrued`
старше 30 дней, ≥$25 на партнёра → `stripe.Transfer(source_transaction=<charge>)` → `paid_out`;
на `charge.refunded` по уже выплаченной — `Transfer.create_reversal`. Сайт: кнопка
«Подключить выплаты» + история выплат в `/dashboard/affiliate`. Сначала гонять в test-mode
(нужны тестовые ключи Stripe — взять у Игоря), потом первая живая выплата.

**Защиты — обязательны, проверять до мержа (просьба Игоря 10-01):**
- IDOR: каждый эндпоинт выплат фильтрует по `user_id` вызывающего (service_role обходит RLS).
  Партнёр не может подключить/увидеть чужой `stripe_account_id` или чужие выплаты.
- Вебхук: только `stripe.Webhook.construct_event` с секретом; `account.updated` берёт аккаунт
  из события, не из тела запроса; повторная доставка не создаёт второй перевод.
- Идемпотентность перевода: `idempotency_key` = id комиссии/пачки + UNIQUE на
  `payouts.stripe_transfer_id`; задача, упавшая на середине, при повторе не платит дважды.
- Порог 30 дней и $25 — в коде задачи, не в UI; `payouts_enabled=false` → не платим.
- Ключи: test/live различать по префиксу, живой ключ не попадает в тесты; тестовые — в `.env`.
- Перед мержем — скил `blast-radius` (роутеры + migrations = стык) и `security-sweep`.
