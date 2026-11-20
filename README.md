# tg_shop_platform

Платформа Telegram-магазинов: multi-tenant и multi-bot. Один экземпляр обслуживает
множество магазинов, у каждого свой бот. Это модульный монолит на Python 3.13.
Управляет платформой **Master Bot** — веб-админки нет. Боты магазинов обслуживает
процесс `bot_worker`.

## Стек

- **Telegram**: aiogram 3, aiogram-dialog.
- **Данные**: SQLAlchemy 2 (asyncio) + Alembic, PostgreSQL 17 + pg_partman (`ops/postgres/Dockerfile`).
- **Кэш**: Redis.
- **Очереди и фоновые задачи**: RabbitMQ + FastStream.
- **Файлы**: MinIO (S3-совместимое хранилище; также поддерживается `STORAGE_BACKEND=s3`).
- **DI**: Dishka. **Конфигурация**: pydantic-settings. **Логи**: structlog.
- **Шифрование секретов**: Fernet (`cryptography`).
- **Наблюдаемость**: агент Grafana Alloy → Loki (логи) и Prometheus (метрики) → Grafana,
  Alertmanager → Telegram; ошибки уходят в urgentry (Sentry SDK), трейсы — OpenTelemetry → Alloy → Tempo.
  Подробнее — [docs/ops/observability.md](docs/ops/observability.md).

## Требования

- Linux-сервер, Docker Engine и плагин Docker Compose v2 (`docker compose`).
- GNU make, bash, coreutils (`realpath`, `mktemp`, `stat`, `install`, `timeout`).
- `openssl` (пароли и `ENCRYPTION_KEY` в `make env`), git.
- `python3` на хосте мониторинга: разбор ответов API urgentry и настройка срока хранения
  (PUT в API urgentry; в образе нет клиента с PUT) в `make obs-up` / `make obs-bootstrap`
  (`ops/scripts/obs_bootstrap.sh`), проверка IP в `make obs-bans UNBAN=<ip>`.
- [uv](https://docs.astral.sh/uv/) нужен **только для разработки**: `make install`, `make check-dev`,
  `make test-unit-dev`, `make lint`, `make env-test`, `make test-integration`, `make test-report`.
  Для развёртывания и `make check` / `make test-unit` (unit-тесты в Docker) uv не нужен.

## Требования к серверам

| Сервер | vCPU | RAM | Диск |
|---|---|---|---|
| Staging (всё на одном VPS) | 2 / 4 | 4 ГБ + swap 2 ГБ / 8 ГБ | 50 / 60 ГБ SSD |
| Production VPS 1 (приложение и данные) | 4 / 8 | 8 / 16 ГБ | 80 / 150+ ГБ NVMe |
| Production VPS 2 (наблюдаемость) | 2 / 4 | 2 ГБ + swap 2 ГБ / 4 ГБ | 30 / 80+ ГБ SSD |

Значения — старт / рекомендуемо; это оценка по замеру в простое, без нагрузочного теста.
Сумма `mem_limit` (VPS 1 ≈ 5.1–5.6 ГБ, VPS 2 ≈ 3.1 ГБ, Staging ≈ 8.2 ГБ) — потолки с overcommit:
она может превышать RAM, запас дают swap и неодновременные пики.
Подробнее — [docs/ops/requirements.md](docs/ops/requirements.md).

## Развёртывание

Два сценария: **всё на одном хосте** (stage, демо) и **VPS 1 (приложение) + VPS 2 (мониторинг)**,
связанные VPN (prod). `.env.example` по умолчанию настроен на **два сервера**
(`ALLOY_SINGLE_HOST=0`, URL мониторинга на `10.10.0.2`) — для одного хоста значения меняются
на шаге 4 сценария 1.

### Требования к хосту

Подробно — [docs/ops/requirements.md](docs/ops/requirements.md). Кратко:

- Docker Engine + плагин `docker compose` v2, `make`, `git`, `openssl` (генерация секретов в `make env`);
- `python3` — на хосте мониторинга (`make obs-up`/`obs-bootstrap`: проверка `URGENTRY_INTERNAL_URL`
  и настройка retention urgentry; `make obs-bans UNBAN=<ip>`); приложению на хосте Python не нужен;
- uv — только для разработки (см. [Требования](#требования));
- синхронизация времени (chrony) и swap.

Пароли в `.env` — только hex (Makefile читает файл через `source`, символ `$` не поддерживается).

### Сценарий 1 — всё на одном хосте

1. **Подготовка хоста** — Docker, make, git, openssl, python3, chrony, swap (см. выше).

2. **Код и теги версий:**

   ```bash
   git clone <URL репозитория> && cd tg_shop_platform
   git fetch --tags
   ```

   Без тега майлстоуна сборка образа (`make up`/`make agent-up`) падает — см.
   [docs/ops/versioning.md](docs/ops/versioning.md).

3. **Создать `.env`:**

   ```bash
   make env
   ```

   Копирует `.env.example`, подставляет случайные пароли (права `600`), генерирует Basic Auth для
   Caddy: `OBS_AUTH_USER=admin` и пароль — **он печатается один раз (блок `SAVE THIS PASSWORD`),
   сохраните его**. Потеряли — `make obs-auth` задаст новый. Существующий `.env` не трогается;
   `make env FORCE=1` перегенерирует все секреты (бэкап `.env.bak.<ts>`) — на рабочих данных не
   использовать: БД, Redis, RabbitMQ продолжат требовать старые пароли, а новый
   `ENCRYPTION_KEY` не расшифрует сохранённые секреты.

4. **Заполнить `.env`** (до следующих шагов):

   | Переменная | Значение |
   |---|---|
   | `MASTER_BOT_TOKEN` | токен мастер-бота (обязательно) |
   | `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` | бот и чат для алертов (без них алерты не отправляются) |
   | `APP_UID`, `APP_GID` | `id -u` / `id -g` владельца каталога данных |
   | `ALLOY_SINGLE_HOST` | `1` |
   | `OBS_LOKI_PUSH_URL` | `http://loki:3100/loki/api/v1/push` |
   | `OBS_PROMETHEUS_REMOTE_WRITE_URL` | `http://prometheus:9090/api/v1/write` |
   | `OBS_TEMPO_OTLP_ENDPOINT` | `tempo:4317` |
   | `CADDY_MODE` | `ip` + `VPS_IP=<публичный IP>` (самоподписанный сертификат) или `domain` + `GRAFANA_DOMAIN`/`URGENTRY_DOMAIN` (Let's Encrypt) |
   | `URGENTRY_INTERNAL_URL` | оставить `http://caddy:8081` |
   | `OBS_INGEST_BIND_ADDRESS` | оставить `127.0.0.1` |

5. **Каталоги данных:**

   ```bash
   make init-dirs
   ```

   Создаёт bind-mount каталоги из `PROJECT_DATA_DIR` (по умолчанию `/opt/tg_shop_platform_data`).
   Если не хватает прав, команда печатает нужные `sudo mkdir`/`chown` — выполните и повторите.

6. **Стек мониторинга:**

   ```bash
   make obs-up
   ```

   Поднимает Loki, Prometheus, Alertmanager, Grafana, Tempo, urgentry, Caddy, CrowdSec, создаёт сеть
   `<PROJECT_NAME>_obs_default`, администратора urgentry и записывает в `.env` `SENTRY_DSN`.

7. **Приложение + агент сбора:**

   ```bash
   make agent-up
   ```

   Собирает образ, поднимает основной стек и Alloy (профиль `observability`), подключает их к
   сети мониторинга (при `ALLOY_SINGLE_HOST=1`; без сети `<PROJECT_NAME>_obs_default`, т.е. без
   `make obs-up`, команда завершится ошибкой) и вызывает `make ready`. Отдельный `make up` не нужен.

8. **Проверка** — раздел [Проверка результата](#проверка-результата).

### Сценарий 2 — VPS 1 (приложение) + VPS 2 (мониторинг)

У каждого хоста свой `.env` (`make env` на каждом). Общих секретов у хостов нет:
Loki/Prometheus/Tempo/urgentry ingest принимают данные без авторизации и защищены только адресом
в VPN; связывает хосты одна строка `SENTRY_DSN`, которую `make obs-up` пишет на VPS 2 и которая
копируется на VPS 1. VPS 2 публичный (Caddy на 80/443): `MASTER_BOT_TOKEN` на нём не задавайте.

0. **Оба хоста**: WireGuard, firewall, chrony, swap — [docs/ops/network.md](docs/ops/network.md).
   Ниже `<VPN-IP VPS 2>` — адрес VPS 2 в туннеле (в примерах `10.10.0.2`).

1. **VPS 2: код, `.env` и мониторинг:**

   ```bash
   git clone <URL репозитория> && cd tg_shop_platform && git fetch --tags
   make env            # сохраните пароль Basic Auth
   ```

   **До** `make obs-up` укажите в `.env` (полный список — [таблица](#переменные-env)):
   `OBS_INGEST_BIND_ADDRESS=<VPN-IP VPS 2>` (интерфейс `wg0` уже должен существовать, иначе
   контейнеры не опубликуют порты), `URGENTRY_INTERNAL_URL=http://<VPN-IP VPS 2>:8081` (только
   приватный адрес: публичный DSN запрещён и приложение с ним не стартует), `CADDY_MODE` +
   `VPS_IP`/домены ([предусловия режима `domain`](#режим-caddy_modedomain)), `ALERT_TELEGRAM_*`,
   `APP_UID`/`APP_GID`. Затем:

   ```bash
   make init-dirs
   make obs-up         # пишет SENTRY_DSN=http://<key>@<VPN-IP VPS 2>:8081/1 в .env
   sed -n 's/^SENTRY_DSN=//p' .env   # значение для VPS 1
   ```

2. **VPS 1: приложение:**

   ```bash
   git clone <URL репозитория> && cd tg_shop_platform && git fetch --tags
   make env
   ```

   В `.env` укажите `MASTER_BOT_TOKEN`, `PROJECT_ENVIRONMENT`, `APP_UID`/`APP_GID`,
   `ALLOY_SINGLE_HOST=0` (значение по умолчанию), `ALLOY_HOST_LABEL`, `OBS_LOKI_PUSH_URL`,
   `OBS_PROMETHEUS_REMOTE_WRITE_URL` и `OBS_TEMPO_OTLP_ENDPOINT=<VPN-IP VPS 2>:4317` на VPN-IP VPS 2,
   а также `SENTRY_DSN` — значение, напечатанное на VPS 2 (можно и позже, затем повторить
   `make agent-up`). Затем:

   ```bash
   make agent-config        # проверка .env и compose
   make init-dirs
   make agent-up            # основной стек + Alloy + make ready
   ```

3. **Проверки** с обеих сторон — [Проверка результата](#проверка-результата).

**Изменение `SENTRY_DSN`**: если после повторного `make obs-up`/`make obs-bootstrap` на VPS 2
изменился `SENTRY_DSN`, перенесите на VPS 1 **только его значение** (`.env` хостов различаются,
копировать файл целиком нельзя):

```bash
sed -n 's/^SENTRY_DSN=//p' .env      # VPS 2: печатает только значение (http://<key>@<VPN-IP VPS 2>:8081/1)
```

На VPS 1 вместо `<значение>` подставьте **только** напечатанное значение (без `SENTRY_DSN=`;
символов `|`, `&`, `\` в DSN нет, поэтому `sed` безопасен) или замените строку в редакторе:

```bash
sed -i 's|^SENTRY_DSN=.*|SENTRY_DSN=<значение>|' .env
make agent-up
```

### Режим `CADDY_MODE=domain`

`GRAFANA_DOMAIN` и `URGENTRY_DOMAIN` должны иметь DNS A-записи на публичный IP хоста мониторинга
(VPS 2 или единственного хоста) **до** `make obs-up`, а порт 80/tcp — быть открыт: Let's Encrypt
проверяет домен по HTTP-01. Без своего домена подойдёт `grafana.<ip>.nip.io`.

### Переменные `.env`

| Переменная | Один хост | VPS 2 (мониторинг) | VPS 1 (приложение) |
|---|---|---|---|
| `PROJECT_ENVIRONMENT` | `stage` | как на VPS 1 | `prod` / `stage` |
| `APP_UID`, `APP_GID`, `PROJECT_DATA_DIR` | свои | свои | свои |
| `MASTER_BOT_TOKEN` | задать | не задавать | задать |
| Пароли БД/Redis/RabbitMQ/MinIO, `ENCRYPTION_KEY` | `make env` | не используются | `make env` |
| `OBS_AUTH_HASH`, `GRAFANA_ADMIN_PASSWORD`, `CROWDSEC_BOUNCER_KEY`, `URGENTRY_BOOTSTRAP_*` | `make env` / `make obs-up` | `make env` / `make obs-up` | не используются |
| `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` | задать | задать | не используются |
| `CADDY_MODE` + `VPS_IP` / `GRAFANA_DOMAIN`, `URGENTRY_DOMAIN` | задать | задать | не используется |
| `OBS_INGEST_BIND_ADDRESS` | `127.0.0.1` | `<VPN-IP VPS 2>` | не используется |
| `URGENTRY_INTERNAL_URL` | `http://caddy:8081` | `http://<VPN-IP VPS 2>:8081` | не используется |
| `SENTRY_DSN` | пишет `make obs-up` | пишет `make obs-up` | копия с VPS 2 |
| `ALLOY_SINGLE_HOST` | `1` | не используется | `0` |
| `ALLOY_HOST_LABEL` | любое | не используется | `vps1` |
| `OBS_LOKI_PUSH_URL` | `http://loki:3100/loki/api/v1/push` | не используется | `http://<VPN-IP VPS 2>:3100/loki/api/v1/push` |
| `OBS_PROMETHEUS_REMOTE_WRITE_URL` | `http://prometheus:9090/api/v1/write` | не используется | `http://<VPN-IP VPS 2>:9090/api/v1/write` |
| `OBS_TEMPO_OTLP_ENDPOINT` | `tempo:4317` | не используется | `<VPN-IP VPS 2>:4317` |

### Проверка результата

```bash
make ready      # хост приложения: /health/ready = 200 у master_bot и bot_worker
make obs-ps     # хост мониторинга: все сервисы running/healthy
# CADDY_MODE=ip (самоподписанный сертификат, поэтому -k)
curl -k -u admin:<пароль Basic Auth> -o /dev/null -w '%{http_code}\n' https://<VPS_IP>/grafana/api/health   # 200
curl -k -o /dev/null -w '%{http_code}\n' https://<VPS_IP>/grafana/api/health                                  # 401
# CADDY_MODE=domain
curl -u admin:<пароль Basic Auth> -o /dev/null -w '%{http_code}\n' https://<GRAFANA_DOMAIN>/api/health        # 200
curl -o /dev/null -w '%{http_code}\n' https://<GRAFANA_DOMAIN>/api/health                                       # 401
make otel-smoke # хост приложения: span → SQL → RabbitMQ → ошибка в urgentry, печатает TRACE_ID
```

urgentry — `https://<VPS_IP>/` (`ip`) или `https://<URGENTRY_DOMAIN>/` (`domain`) (Basic Auth, затем логин `URGENTRY_BOOTSTRAP_EMAIL` и пароль
`URGENTRY_BOOTSTRAP_PASSWORD` из `.env` хоста мониторинга); ошибка из `make otel-smoke` должна
появиться в проекте, трейс — в Grafana (Tempo) по `TRACE_ID`.

### Остановка, обновление, откат

- **Остановка** (один хост — строго в этом порядке):
  `make agent-down` → `make down` → `make obs-down`. Если после этого осталась сеть
  `<PROJECT_NAME>_docker_api` — `docker network rm <PROJECT_NAME>_docker_api`. На двух хостах:
  VPS 1 — `make agent-down`, `make down`; VPS 2 — `make obs-down`.
- **Обновление**: `git pull && git fetch --tags`, затем `make env-check TEST_ENV_FILE=.env`
  (на боевом хосте нет `.env.test`, а без этого параметра цель проверяет и его; новые ключи
  из `.env.example` дописать вручную, на двух хостах — на том хосте, которому они нужны), затем
  `make agent-up` (хост приложения; миграции применяет `db_update`) и `make obs-up` (хост
  мониторинга). `make env FORCE=1` не используйте: он сгенерирует заново **все** секреты, а
  пароли БД и `ENCRYPTION_KEY` разойдутся с данными.
- **Откат**: `git checkout <предыдущий тег>` + `make agent-up`; миграции БД автоматически не
  откатываются — порядок восстановления в [docs/ops/recovery.md](docs/ops/recovery.md).

Caddy/CrowdSec (баны, смена ключа bouncer, проверка Caddyfile), обновление `.env`, созданного
старой версией, и устройство стека мониторинга — [docs/ops/observability.md](docs/ops/observability.md).

## Локальная разработка

`make install` (uv + pre-commit), затем `make env` и в `.env`: `PROJECT_ENVIRONMENT=loc`,
`PROJECT_DATA_DIR` в домашнем каталоге (абсолютный путь без `$`, например
`/home/<user>/tg_shop_platform_data` — иначе `make env-test`/`make test-integration` создадут
`/opt/tg_shop_platform_data_test` и потребуют `sudo`), `VPS_IP=127.0.0.1` и single-host значения
из шага 4 сценария 1. Дальше — как в сценарии 1 (`make init-dirs` → `make obs-up` → `make agent-up`).
Тесты: `make check-dev`, `make test-integration`.

## Тесты

| Команда | Что делает | Нужен uv |
|---|---|---|
| `make check` | unit-тесты в Docker (`make test-unit`) | нет |
| `make test-unit` | unit-тесты в Docker: стадия `test` Dockerfile, код монтируется в контейнер только для чтения; `make test` — её алиас | нет |
| `make check-dev` | линтеры (`make lint`) + проверка форматирования (`make format-check`) + unit-тесты (`make test-unit-dev`) | да |
| `make test-unit-dev` | unit-тесты на хосте (`uv run pytest tests/unit`) | да |

Отдельный файл или аргументы pytest: `make test-unit UNIT_TESTS=tests/unit/test_encrypted_string.py PYTEST_ARGS=-v`
(так же для `make test-unit-dev`).

## Полезные команды

| Команда | Что делает |
|---|---|
| `make help` | список всех целей |
| `make down` | остановить основной стек |
| `make migrate` | применить миграции БД |
| `make env-test` | сгенерировать `.env.test` (изолированные данные, порты +20000) |
| `make test-integration` | поднять тестовую инфраструктуру и прогнать интеграционные тесты |
| `make check` / `make check-dev` | unit-тесты в Docker / линтеры + форматирование + unit-тесты через uv (см. [Тесты](#тесты)) |
| `make test-alerts` | проверить правила алертов Prometheus (promtool в Docker) |
| `make test-report` | unit + интеграционные тесты + promtool → отчёт `build/reports/milestone-I-tests.md` |

Отчёты о сдаче этапов — [docs/reports/](docs/reports/): результаты автотестов вехи I
([milestone-I-tests.md](docs/reports/milestone-I-tests.md)), сценарий демонстрации
([milestone-I-demo.md](docs/reports/milestone-I-demo.md)) и исправленные замечания повторной сдачи
([milestone-I-fixes.md](docs/reports/milestone-I-fixes.md)). Отчёт для сдачи: закоммитить код,
на чистом дереве выполнить `make test-report REPORT_MD=docs/reports/milestone-I-tests.md` и закоммитить
отчёт отдельным коммитом (в отчёте указан проверенный коммит).

Документация: восстановление — [docs/ops/recovery.md](docs/ops/recovery.md),
версионирование — [docs/ops/versioning.md](docs/ops/versioning.md).
