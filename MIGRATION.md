# Переезд бэкенда афиши: Supabase → Yandex Cloud

Статус: **переключено 29.09.2026 (этап 3).** Сайт `2026-09-29-1` работает на Yandex Cloud.
В Supabase `afisha_save` отвечает `read_only` всем (миграция `afisha_save_read_only_moved_to_yandex`),
данные там — копия на момент переключения, неделю держим как запасную.

**Откат (если что-то не так):** вернуть в `index.html` `YC_URL = ""` и прежнюю `SITE_VERSION`; в Supabase
выполнить `yc/supabase_rollback.sql` (прежнее тело `afisha_save`).
Правки, сделанные уже в Яндексе, перед этим забрать через `afisha-admin` (`get_state`).

Функция `afisha-api`: https://functions.yandexcloud.net/d4e8n3j9esf1uq386ik3

Не сделано на этапе 2: замер RU опроса (смотреть в консоли: YDB → Мониторинг → Request units).

Коды взяты из `afisha_access` через Supabase (по решению владельца) и попали в журнал сессии Claude.
После переезда их стоит сменить.

До supabase.co из среды Claude прокси не пускает, поэтому `copy`/`verify` читают Supabase через `afisha-admin`.

Почему две функции: прокси среды Claude пропускает только HTTPS на 443, а YDB — это gRPC на 2135.
Поэтому таблицы и перенос данных делает закрытая `afisha-admin` изнутри облака; вызвать её можно только
с ролью на каталог. Коды доступа в неё приходят уже хешем.

## Что уже сделано (код в `yc/`)
- `yc/function/` — Cloud Function: `logic.py` (ответы как у RPC Supabase, коды по scrypt-хешу, опрос `since` → `unchanged`),
  `store.py` (YDB, сериализуемая транзакция при сохранении), `index.py` (HTTP, CORS, `text/plain`),
  `admin.py` (закрытая служебная: схема, запись кодов-хешей и данных).
- `yc/schema.yql` — таблицы `access`, `state`, `meta`.
- `yc/tests/test_logic.py` — 17 проверок без облака: `python3 -m unittest discover -s yc/tests`.
- `yc/deploy.py` — `check` / `setup` / `deploy` / `copy` / `verify` / `set-events`; `verify` — сверка с Supabase и проверки этапа 2.
- `index.html` — переключатель `YC_URL`: пусто → Supabase (как сейчас), адрес функции → Yandex Cloud.
  Проверено в Chromium на локальной копии функции: вход, опрос без лишнего OPTIONS, сохранение; в режиме Supabase запросы прежние.

## Что осталось по шагам
1. `pip install pyjwt cryptography requests "ydb>=3.18,<4"`, затем `python3 yc/deploy.py check`, потом `setup`.
   Роль `editor` не даёт выдавать роли: если `setup` об этом скажет — выдать `afisha-fn` роль `ydb.editor` в консоли и повторить `setup`.
2. `AFISHA_CODE_EDITOR=… AFISHA_CODE_VIEWER=… python3 yc/deploy.py copy`, затем `… verify`; RU опроса — в мониторинге YDB.
3. Переключение: снова `copy` (свежие данные) → в Supabase `afisha_save` вернуть `read_only` всем → в `index.html`
   `YC_URL` = адрес функции, поднять `SITE_VERSION` → `set-events` с той же `site.version`.

## Цель
Афиша — независимый проект, без общей базы с другим проектом в Supabase.
Сайт остаётся на GitHub Pages, бэкенд — в Yandex Cloud, в пределах бесплатного объёма.

## Как сейчас (Supabase, проект `hyjyfmgskcjijyyblvtg`)
- `afisha_access(code, role editor|viewer, label)` — два кода: «Организатор» (editor), «Куратор» (viewer).
- `afisha_state(id, data jsonb, updated_at, updated_by)` — строки `akusha` (schools/seats/plan/history, ~11 КБ) и `events` (venues/events/site, ~5 КБ).
- RPC `afisha_load(p_code)` → `{ok, role, label, data, updated_at, updated_by, afisha, afisha_updated_at}` или `{ok:false, error:'bad_code'}`.
- RPC `afisha_save(p_code, p_data, p_since)` → `ok` / `bad_code` / `read_only` / `conflict` (+ текущие данные).
- В той же базе живёт другой проект. Трогать **только** четыре объекта: `afisha_access`, `afisha_state`, `afisha_load`, `afisha_save`.

## Как будет (Yandex Cloud)
- Облако `cloud-alien-aliev`, отдельный каталог `afisha` (ID `b1gi8forub14au4jm25l`; не `default` — там чужие ресурсы). Каталог создан.
- YDB serverless `afisha-db`: таблицы `access` (salt, scrypt-хеш кода, role, label), `state` (id, data, updated_at, updated_by), `meta` (id, updated_at — для дешёвого опроса).
- Сервисный аккаунт `afisha-fn` — только `ydb.editor`; под ним работает функция.
- Cloud Function `afisha-api` (Python), публичная, без API Gateway. Ответы в том же формате, что у Supabase RPC.
- В `index.html`: адрес бэкенда, запрос `text/plain` (без preflight), опрос с `since` → ответ «без изменений». Интерфейс не меняется.

## Расчёт нагрузки (опрос раз в 25 с, только видимая вкладка после входа)
- Реально: 3 чел × 4 ч/день × 22 дня ≈ 38 000 вызовов/мес.
- Худший случай: 3 вкладки на экране круглосуточно ≈ 311 000 вызовов/мес.
- Cloud Functions free: 1 000 000 вызовов — хватает. API Gateway free: 100 000 — не используем.
- YDB free: 1 000 000 RU — замерить стоимость опроса на этапе 2; если > 2 RU, интервал 40–60 с.

## Этапы
0. Вручную: каталог `afisha`, СА `afisha-deploy` (`editor` + `functions.admin` на каталог), авторизованный ключ → переменные среды `YC_KEY_ID`, `YC_SA_ID`, `YC_PRIVATE_KEY` (форма среды не принимает многострочный JSON; в `YC_PRIVATE_KEY` переносы могут быть как `\n`-экранами, так и настоящими — обработать оба случая; после переезда переменные удалить, ключ в консоли отозвать), разрешить домены `*.yandexcloud.net`, `api.cloud.yandex.net`, `*.api.cloud.yandex.net`, бюджет 100 ₽.
1. Поднять YDB, таблицы, `afisha-fn`, функцию; залить копию данных, коды — хешами.
2. Проверить на копии: побайтное совпадение данных, 11 регрессионных проверок, неверный код, куратор не пишет, конфликт, CORS, замер RU.
3. Переключить (порядок выше): свежая копия данных → старый `afisha_save` в Supabase на «только чтение» → новый `index.html` + `SITE_VERSION` и `site.version`.
4. Неделю держать старые таблицы. Удалить четыре объекта **только после подтверждения владельца**.
5. Обновить README: где что лежит, как обновлять мероприятия, как делать резервную копию.
6. Удалить ключ `afisha-deploy` (для работы сайта не нужен).

## Продажи из кассы kassir.ru (с 29.09.2026)
- Функция `afisha-kassa` (закрытая, `yc/function/kassa.py`) по таймеру `afisha-kassa-timer` каждые 30 минут
  с 7:00 до 23:30 МСК заходит на new-report.kassir.ru, заказывает один «Отчёт по продажам (организатор)» по всем
  событиям и пишет в YDB строку `kassa`: по каждому событию дата, время, квота, свободно, продано, возвраты.
- Сайт сопоставляет события по дате и времени и показывает «Продано по кассе» сам; «школа не указана» = касса − вписанное у школ.
- Логин и пароль кассы — только в переменных функции `KASSIR_LOGIN`, `KASSIR_PASSWORD` (вписывает владелец в консоли).
  `deploy.py` код функции кассы перевыкладывает только при её создании, чтобы не стереть их.
- Проверить вручную: `python3 yc/deploy.py kassa-now`.
