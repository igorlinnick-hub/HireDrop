# answers-at-signup — вопросы работодателей один раз, при регистрации

Обновлено: 2026-10-01 · ветка: main (HireDrop #293 смержен) + website `feat/answers-at-signup` (#228 открыт)

## Состояние

Список вопросов — один: `modules/employer_answers.py` (`QUESTIONS`, `OPT_OUT`, `SINCE`).
В проде бэкенд: `GET /profile/employer-answers` (все вопросы + ответы в профиле),
`POST …/suggest` (подсказки из резюме), `POST /profile/employer-answers` (сохранение).
Новые вопросы: `school`, `degree`, `salary_expectation`; «у меня этого нет» (`no_linkedin`,
`no_degree`, `no_salary_expectation`) едет вместе с вопросом (`opt_out`). Колонки в `profiles`
применены (`migrations/add_employer_answer_fields.sql`).

Сайт (#228, НЕ смержен): шаг 7 «Answers» в визарде (11 шагов), та же форма в гейте Start,
Education + Salary expectation в Settings, `/preview/answers` (`?dark=1`).

**Пока #228 не смержен, для юзеров не изменилось ничего:** старый клиент не называет
`answers_ui`, и сервер держит его на старом списке из 8 вопросов.

## Последний заход (10-01)

- Вопросы переехали из «стены перед первым Start» в регистрацию; гейт остался страховкой.
- Подсказки вместо догадок: структура ATS-резюме бесплатно, иначе один вызов Haiku по PDF
  (`modules/ai_resume_facts.py`), значение обязано стоять в резюме дословно и подходить полю.
  Фильтр `salary_min` только ПРЕДЛАГАЕТСЯ как зарплатное ожидание. Молча ничего не пишется.
- `answers_ui` (сайт шлёт `=2` в readiness / start / save): вопрос считается пропущенным только
  для клиента, который умеет его нарисовать. Иначе старая вкладка без галочки «нет диплома»
  держала бы Start закрытым, пока юзер не впишет «N/A» — и это уехало бы в заявки.
- Рой-скептик воспроизвёл 14 дефектов первой версии (2 HIGH: ловушка старого клиента и возврат
  квоты после платного вызова в `/suggest`); все закрыты тестами из его случаев.
- Проверено вживую на тестовом аккаунте (удалён): визард 1→7→8, гейт Start, старый/новый клиент.

## Сломано / не доделано

- **Мерж #228 = Start на новом сайте попросит у каждого старого аккаунта 3 ответа** (и `drive.py`
  на аккаунте Игоря встанет на `employer_answers`, пока он не ответит). Поэтому не смержен.
- У шага «Answers» шапка заимствована у шага 1 (`step-1.jpg`) — нужен свой рендер.
- «Не живу в США» теперь останавливает регистрацию на шаге 7 (раньше — только первый Start).
  Продуктовое решение за Игорем.
- Расширение не читает `profile.school` / `degree` / `salary_expectation` — потребитель пока
  только ночная смена. Колонка нарочно не `desired_salary`: её `content.js` читает парсером
  «только цифры».
- `background.js` глотает любой отказ `POST /campaign/start` (`catch {}`): старт из попапа
  игнорирует 403 гейта. Хвост #271, ext-лейн.
- Кэш подсказок из резюме — в памяти процесса (2 воркера = до 2 чтений на резюме).

## Следующий шаг

Игорь смотрит `/preview/answers` (день и `?dark=1`) на превью #228 и говорит «мержи» — после
мержа отвечает на три вопроса при первом Start. Следующую сессию начинать моделью **Opus**.

Файлы: бэкенд — `modules/employer_answers.py`, `modules/ai_resume_facts.py`,
`app/routers/{profile,campaign}.py`, `app/db/{profile,campaign}.py`; сайт (ветка
`feat/answers-at-signup`) — `components/dashboard/EmployerAnswersForm.tsx`,
`components/onboarding/{StepEmployerAnswers,OnboardingWizard}.tsx`, `lib/employerAnswers*.ts`,
`lib/onboarding/steps.ts`, `app/preview/answers/`.
