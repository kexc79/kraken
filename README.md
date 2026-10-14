# Kraken / Pharma

Контейнер для `as_3.py`: Python 3.12, Oracle Instant Client 19.32 Basic
(Thick mode), зависимости Python с фиксированными версиями.
Развёртывание рассчитано на Ubuntu x86-64 с Docker и Compose v2.

## Развёртывание на Ubuntu через git clone

Нужны Git, Docker Engine и Docker Compose v2 или новее. Репозиторий приватный:
для клонирования используйте свой GitHub SSH-ключ или HTTPS с авторизацией.

```bash
git clone git@github.com:kexc79/kraken.git
cd kraken
bash ./kraken.sh init
nano config/.env
nano config/.req
# Положите Google JSON по пути config/google-service-account.json.
chmod 600 config/.env config/.req config/google-service-account.json
./kraken.sh start
```

`init` создаёт конфиги из `.env.example` и `.req.example`, если их ещё нет,
и устанавливает право запуска `kraken.sh`. Существующие конфиги сохраняются.
Первый `start` собирает контейнер и выводит логи в терминал. Пароли и JSON
остаются в `config/` на сервере; файл `.env.deploy` для такого запуска не нужен.
`Ctrl+C` останавливает обработку.

Для обновления остановите текущую задачу и выполните:

```bash
git pull --ff-only
./kraken.sh start
```

Команда пересобирает образ и заново читает конфиги. Папки `config/` и `logs/`
не входят в Git и сохраняются при обновлении. Саму обработку можно остановить
из другого терминала командой `./kraken.sh stop`.

## Первичная настройка из репозитория

В репозитории находятся исходники и обезличенные примеры конфигов. Реальные
`.env`, `.req`, `.env.deploy`, Google JSON, логи и служебные каталоги исключены.

В новом локальном клоне создайте конфиги из шаблонов:

```powershell
Copy-Item .env.example .env
Copy-Item .req.example .req
Copy-Item .env.deploy.example .env.deploy
python -m pip install -r requirements-deploy.txt
```

Заполните свои значения и положите `google-service-account.json` рядом с ними.
Затем выполните `.\kraken.ps1 deploy`. На VPS конфиги появятся в `config/`;
повторный деплой сохраняет существующие конфиги. Не копируйте шаблоны поверх
уже настроенных конфигов. Запуск: `.\kraken.ps1 start` с локального компьютера
или `./kraken.sh start` из каталога проекта на VPS.

## Файлы на сервере

Каталог из `DEPLOY_HOME` в локальном `.env.deploy`; текущее значение — `/kraken`:

```text
/kraken/
  kraken.sh
  compose.yaml
  Dockerfile
  as_3.py
  config/
    .env
    .req
    google-service-account.json   # ключ сервисного аккаунта Google
    oracle/                         # необязательно: tnsnames.ora, sqlnet.ora, wallet
  logs/
    pharma_match.log
    sql_debug.log
```

`.env` содержит настройки Google Sheets, `.req` — реквизиты Oracle и OpenAI.
Это **не** файл pip requirements: зависимости перечислены в `requirements.txt`.
Файлы из `config/` подключаются к контейнеру только для чтения и заново
читаются при каждом запуске. Редактировать их можно прямо на сервере:

```bash
cd /kraken
nano config/.env
nano config/.req
chmod 600 config/.env config/.req config/*.json
./kraken.sh check
./kraken.sh start
```

`GSERVICE_JSON` — имя JSON-файла сервисного аккаунта Google относительно
`config/`, например `google-service-account.json`. Можно указать путь
внутри контейнера, например `/config/google.json`. Локальный путь Windows
или произвольный путь на сервере внутри контейнера недоступен.
Откройте доступ к Google-таблице для адреса `client_email` из JSON с правом
редактирования. `OPENAI_API_KEY` из `.req` обслуживает OpenAI и не заменяет
JSON-ключ Google.

Конфигурация читается как dotenv, без интерполяции `${...}`. Значения с пробелами
и `#` заключайте в кавычки. Секреты не добавляются в образ и не передаются
через Docker environment. Доступ к каталогу и секретам ограничен владельцем.
`.env.deploy` остаётся только на локальном компьютере.

Для трёх `WORKSHEET_NAME_*` дополнительно поддерживаются ссылки на имя другого
листа: `WORKSHEET_NAME_MAIN + "_непривязавшиеся"` и
`${WORKSHEET_NAME_MAIN}_неоднозначности`. Код разрешает только имена этих
переменных, строковые суффиксы и конкатенацию; произвольный Python не выполняется.
К паролям и ключам эта обработка не применяется.

## Управление с локального Windows-компьютера

Запускайте из папки проекта. Нужен Python; зависимости утилиты:

```powershell
python -m pip install -r requirements-deploy.txt
.\kraken.ps1 deploy   # загрузить код и недостающие конфиги; ничего не запускает
.\kraken.ps1 build    # собрать образ на Ubuntu
.\kraken.ps1 check    # проверить конфиги и Oracle Client без сетевых запросов к сервисам
.\kraken.ps1 start    # пересобрать и запустить на VPS с живыми логами
.\kraken.ps1 status
.\kraken.ps1 logs
.\kraken.ps1 stop
```

Также работает `kraken.cmd start` из cmd.exe. Без аргумента выполняется `start`.
Реквизиты VPS берутся из `.env.deploy`: `vps_ip`, `vps_user`, `vps_password`,
`DEPLOY_HOME`; необязательный `vps_port` задаёт порт. Также поддерживаются
прежние `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PASSWORD`, `DEPLOY_PORT`.
SSH-ключ сервера сверяется с системным known_hosts и `.deploy/known_hosts`.
Новый сервер запоминается при первом подключении; смена ключа вызывает ошибку.
При создании недоступного каталога `DEPLOY_HOME` утилита использует sudo
с паролем SSH через stdin.

Повторный `deploy` обновляет код и сохраняет существующие конфиги на сервере.
Чтобы **явно перезаписать** серверные `.env`, `.req` и имеющийся локально
JSON-ключ, используйте `.\kraken.ps1 deploy -UpdateConfig`.
Команда `start` сама вызывает сборку; перед новым запуском завершите текущий.
Работающий контейнер продолжает использовать образ предыдущей сборки.

## Управление на Ubuntu

```bash
cd /kraken
./kraken.sh build
./kraken.sh check
./kraken.sh start   # пересборка, проверка и запуск с живыми логами
./kraken.sh status
./kraken.sh logs    # отдельно подключиться к логам
./kraken.sh stop
./kraken.sh run     # синоним start
```

`start`/`run` выполняют сборку, проверяют конфигурацию и запускают один проход
скрипта в текущем терминале с живыми логами и кодом завершения приложения.
`Ctrl+C` при запуске через `kraken.sh` или `kraken.ps1` останавливает задачу.
Соединение SSH нужно держать открытым до завершения; для длительной работы
на VPS можно запустить команду внутри `tmux` или `screen`.
Сборка использует кэш Docker; конфиги всегда читаются из смонтированного
`config/`, поэтому не попадают в образ и не требуют копирования в него.
Повторный запуск во время обработки отклоняется. Автозапуска и расписания нет;
после завершения контейнер остаётся доступен для `status` и `logs`.
Контейнер работает с UID/GID пользователя запуска и не публикует сетевых портов.
Запускайте `kraken.sh` от владельца каталога проекта.
Изменения конфигурации применяются при следующем запуске. Чтобы изменить
параметры текущей задачи, выполните `stop`, отредактируйте файлы и запустите снова.

## Контейнер на локальном компьютере

Для выполнения самого контейнера на Windows установите Docker Desktop
с Linux containers. Флаг `-Local` переключает команды на локальный Docker:

```powershell
.\kraken.ps1 build -Local
# При первом check/start/run файлы .env и .req копируются в config/,
# если там их ещё нет. Добавьте туда JSON Google и меняйте настройки там.
.\kraken.ps1 check -Local
.\kraken.ps1 start -Local
.\kraken.ps1 logs -Local
.\kraken.ps1 stop -Local
```

Локальное выполнение требует сетевого доступа к Oracle, Google и OpenAI.
Для удалённого управления VPS
через обычные команды `kraken.ps1` локальный Docker не требуется.

## Проверка и ограничения

`check` проверяет импорты, загрузку Oracle Client, наличие обязательных настроек
и разбирает закрытый ключ Google. Это офлайн-проверка: она не изменяет Google
Sheets и не вызывает OpenAI или Oracle. Полный `start` выполняет исходную
обработку и записывает результаты в Google Sheets; повторные проходы могут
добавлять те же строки повторно, как в исходном скрипте.

Oracle Client 19 выбран для совместимости в том числе с Oracle Database 11.2+;
дистрибутив Basic поддерживает расширенные кодировки. Ссылка и SHA256 взяты
с [официальной страницы Oracle](https://www.oracle.com/database/technologies/instant-client/linux-x86-64-downloads.html).
Алгоритм сопоставления, запросы и размер партии исходного скрипта сохранены.
Логи приложения содержат адреса/ИНН; файлы в `logs/` сохраняются между запусками.
Docker stdout ротируется (3 файла по 10 МБ); файловые логи приложения не ротируются.

Если Oracle возвращает `ORA-01017`, проверьте `ORACLE_USER` и `ORACLE_PASSWORD`
в серверном `config/.req`, а также правильность базы/сервиса в `ORACLE_DSN`.
Команда `check` работает офлайн и не проверяет действительность пароля в базе.
