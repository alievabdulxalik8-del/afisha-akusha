# Переезд бэкенда афиши: Supabase → Yandex Cloud

Статус: **ждём ручные шаги в консоли** (каталог, сервисный аккаунт, ключ, доступ к сети).
Сайт пока работает на Supabase, ничего не переключено.

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
3. Переключить: свежая копия данных → старый `afisha_save` в Supabase на «только чтение» → новый `index.html` + `SITE_VERSION` и `site.version`.
4. Неделю держать старые таблицы. Удалить четыре объекта **только после подтверждения владельца**.
5. Обновить README: где что лежит, как обновлять мероприятия, как делать резервную копию.
6. Удалить ключ `afisha-deploy` (для работы сайта не нужен).
