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
- GNU make, bash, coreutils (`realpath`, `mktemp`, `stat`, `install`).
- `openssl` (пароли и `ENCRYPTION_KEY` в `make env`), git.
- `python3` на хосте: UUID в `make obs-up` и настройка срока хранения urgentry в
  `make obs-bootstrap` (PUT в API urgentry; в образе нет клиента с PUT).
- Только для разработки: [uv](https://docs.astral.sh/uv/) — `make install`, `make check`,
  `make env-test`, `make test-integration`. Для развёртывания uv не нужен.

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

1. Клонировать репозиторий:

   ```bash
   git clone <URL репозитория>
   cd tg_shop_platform
   ```

2. Создать каталог данных (один раз, нужен sudo):

   ```bash
   sudo mkdir -p /opt/tg_shop_platform_data && sudo chown -R $(id -u):$(id -g) /opt/tg_shop_platform_data
   ```

   Владелец каталога должен совпадать с `APP_UID`/`APP_GID` в `.env` (по умолчанию `1000:1000`):
   приложение в контейнерах работает от этого uid и пишет в `LOGS_DATA_DIR`. Если ваш
   `id -u` не равен 1000, исправьте `APP_UID`/`APP_GID` в `.env`.

3. Создать `.env`:

   ```bash
   make env
   ```

   Шаг можно пропустить: `make init-dirs` (а через него `make migrate`, `make obs-up`,
   `make agent-up`) сам вызовет `make env`, если `.env` ещё нет. Команда копирует
   `.env.example`, подставляет случайные пароли (`change_me*`, `ENCRYPTION_KEY`,
   `POSTGRES_MONITORING_PASSWORD`) и ставит на файл права `600`. Существующий `.env` она
   не трогает. Также генерируется Basic Auth для Caddy: `OBS_AUTH_USER=admin` и случайный
   пароль — **он выводится один раз, сохраните его** (в `.env` только base64 bcrypt-хеш
   `OBS_AUTH_HASH`; без docker хеш останется пустым — выполните `make obs-auth`; новый пароль —
   тоже `make obs-auth`). После этого заполните вручную: `MASTER_BOT_TOKEN` (обязательно), а также
   `ALERT_TELEGRAM_BOT_TOKEN` / `ALERT_TELEGRAM_CHAT_ID`, режим доступа к мониторингу
   (`CADDY_MODE=ip` + `VPS_IP` — `https://<VPS_IP>/` urgentry и `https://<VPS_IP>/grafana/`,
   самоподписанный сертификат, браузер покажет предупреждение; или `CADDY_MODE=domain` +
   `GRAFANA_DOMAIN` / `URGENTRY_DOMAIN` с Let's Encrypt, подойдёт `grafana.<ip>.nip.io`) и URL
   для Alloy (см. ниже). Вход: Basic Auth → затем логин Grafana/urgentry. Для локальной
   разработки поставьте `PROJECT_ENVIRONMENT=loc` и `VPS_IP=127.0.0.1`.

   > ⚠️ Пароль Basic Auth печатается **один раз** (блок `SAVE THIS PASSWORD`). Если `.env` создан
   > неявно (`make init-dirs`, `make obs-up`, `make migrate`, ...), блок легко пропустить в
   > длинном выводе, а в CI он попадёт в лог — в CI создавайте `.env` заранее. Потеряли пароль —
   > задайте новый: `make obs-auth`.

   > ⚠️ `make env FORCE=1` сначала генерирует новый файл в `.env.new`, затем сохраняет текущий
   > в `.env.bak.<YYYYmmddHHMMSS>` (права 600, не перезаписывается) и заменяет его.
   > Если данные уже инициализированы, PostgreSQL, Redis и RabbitMQ продолжат требовать
   > **старые** пароли, а новый `ENCRYPTION_KEY` не расшифрует уже сохранённые секреты.
   > На рабочих данных `FORCE=1` не используйте.

   **Обновление существующего .env** (созданного до появления Basic Auth в Caddy): допишите
   переменные из `.env.example`, иначе `make obs-up` / `make obs-config` / `make obs-auth`
   откажутся работать (молчаливого перехода на режим `ip` нет):
   - `CADDY_MODE=domain` — если мониторинг уже открывался по `GRAFANA_DOMAIN` / `URGENTRY_DOMAIN`
     (оставьте их), или `CADDY_MODE=ip` + `VPS_IP=<публичный IP VPS 2>`;
   - `OBS_AUTH_USER=admin` и пустой `OBS_AUTH_HASH=`, затем `make obs-auth` (пароль показывается
     один раз) и `make obs-up`;
   - `SENTRY_DSN` с публичным хостом (например `urgentry.localhost` или публичный домен)
     теперь отвергается при запуске приложения: очистите его и выполните `make obs-bootstrap`
     (DSN строится из `URGENTRY_INTERNAL_URL`, только приватный IP или имя Docker-сервиса).
   - `CROWDSEC_BOUNCER_KEY=<вывод openssl rand -hex 32>`, `OBS_CADDY_LOGS_DIR`, `OBS_CROWDSEC_DATA_DIR`
     (см. `.env.example`) — **до** любых obs-команд (иначе падают даже `make obs-down`/`obs-ps`),
     затем `make init-dirs && make obs-up`. `make env FORCE=1` здесь не используйте — он
     перегенерирует все секреты.

   **Защита публичного Caddy:** запросы на чужие адреса сбрасываются (catch-all), CrowdSec
   по access-логу Caddy блокирует сканеры и подбор пароля Basic Auth (бан 4 ч); список банов —
   `make obs-bans`, снять — `make obs-bans UNBAN=<ip>` (не баньте вручную IP шлюза Docker
   `172.x.0.1` — отрежете весь локальный доступ). Смена ключа bouncer — `make obs-crowdsec-rekey`,
   проверка Caddyfile — `make obs-validate`. Подробнее: [docs/ops/observability.md](docs/ops/observability.md).

4. **Stage/prod на двух серверах** (VPS 1 — приложение, VPS 2 — наблюдаемость; сеть между
   ними — VPN, см. [docs/ops/network.md](docs/ops/network.md)):
   - VPS 2: **до** `make obs-up` укажите в `.env` `OBS_INGEST_BIND_ADDRESS=<VPN-IP VPS 2>`
     (на этом адресе публикуются Loki, Prometheus, Tempo 4317 и ingest urgentry 8081),
     `URGENTRY_INTERNAL_URL=http://<VPN-IP VPS 2>:8081` (только приватный адрес: публичный
     DSN запрещён и приложение с ним не стартует), `CADDY_MODE` и `VPS_IP`/домены. Затем `make obs-up`:
     поднимется стек наблюдаемости, в urgentry будет создан администратор, а в `.env` запишется
     `SENTRY_DSN` с хостом из `URGENTRY_INTERNAL_URL`.
   - VPS 1: `ALLOY_SINGLE_HOST=0` (значение по умолчанию). В `OBS_LOKI_PUSH_URL` и
     `OBS_PROMETHEUS_REMOTE_WRITE_URL` и `OBS_TEMPO_OTLP_ENDPOINT=<VPN-IP VPS 2>:4317` укажите VPN-IP VPS 2 и выполните `make agent-up`:
     поднимется основной стек и Alloy, после чего команда дождётся готовности ботов.
     Если нужна отправка ошибок, скопируйте `SENTRY_DSN` из `.env` VPS 2 в `.env` VPS 1
     (можно и позже) и повторите `make agent-up`.

   Подробнее — [docs/ops/observability.md](docs/ops/observability.md).

5. **Один хост** (stage/локально): в `.env` выставьте `ALLOY_SINGLE_HOST=1` и укажите URL
   `http://loki:3100/loki/api/v1/push` и `http://prometheus:9090/api/v1/write` (закомментированы
   в `.env.example`). Затем сначала `make obs-up`, потом `make agent-up`: Alloy подключается
   к сети obs-стека, поэтому она должна уже существовать. Приложение шлёт ошибки на
   `caddy:8081` (внутренний ingest → urgentry), трейсы — на `alloy:4317` → `tempo:4317`
   (`OBS_TEMPO_OTLP_ENDPOINT=tempo:4317`). Отключить трейсинг: `OTEL_SDK_DISABLED=true`.
   Проверка цепочки: `make otel-smoke`.
   Порядок при `ALLOY_SINGLE_HOST=1`: `make init-dirs` → `make obs-up` → `make up` → `make agent-up` → `make ready` (`init-dirs` и `ready` уже входят в
   `obs-up`/`agent-up`, `make up` не обязателен): `obs-up` создаёт сеть `<PROJECT_NAME>_obs_default`
   и пишет `SENTRY_DSN`; `make up` без obs-стека поднимает ботов без obs-сети (WARN), подключает их `make agent-up`.
   Остановка — строго `make agent-down` → `make down` → `make obs-down`, см. [порядок остановки](docs/ops/observability.md#запуск-на-одном-хосте-devtest).

6. **Локальная разработка**: в `.env` поставьте `PROJECT_ENVIRONMENT=loc` и
   `PROJECT_DATA_DIR` в домашнем каталоге (например `/home/<user>/tg_shop_platform_data`;
   путь должен быть абсолютным и без `$`). Иначе `make env-test` / `make test-integration`
   создадут `/opt/tg_shop_platform_data_test`, а для этого понадобится `sudo mkdir` + `chown`.
   Для наблюдаемости на той же машине используйте `ALLOY_SINGLE_HOST=1` и single-host URL из п. 5.

7. Проверить готовность: `make ready` ждёт ответа `/health/ready` = 200 от `master_bot` и
   `bot_worker`.

## Полезные команды

| Команда | Что делает |
|---|---|
| `make help` | список всех целей |
| `make down` | остановить основной стек |
| `make migrate` | применить миграции БД |
| `make env-test` | сгенерировать `.env.test` (изолированные данные, порты +20000) |
| `make test-integration` | поднять тестовую инфраструктуру и прогнать интеграционные тесты |
| `make check` | линтеры + проверка форматирования + unit-тесты |
| `make test-alerts` | проверить правила алертов Prometheus (promtool в Docker) |
| `make test-report` | unit + интеграционные тесты + promtool → отчёт `build/reports/milestone-I-tests.md` |

Отчёты о сдаче этапов — [docs/reports/](docs/reports/): результаты автотестов вехи I
([milestone-I-tests.md](docs/reports/milestone-I-tests.md)) и сценарий демонстрации
([milestone-I-demo.md](docs/reports/milestone-I-demo.md)). Отчёт для сдачи: закоммитить код,
на чистом дереве выполнить `make test-report REPORT_MD=docs/reports/milestone-I-tests.md` и закоммитить
отчёт отдельным коммитом (в отчёте указан проверенный коммит).

Документация: восстановление — [docs/ops/recovery.md](docs/ops/recovery.md),
версионирование — [docs/ops/versioning.md](docs/ops/versioning.md),
работа с репозиторием (ветки, PR, с вехи II) — [docs/ops/git-workflow.md](docs/ops/git-workflow.md).
