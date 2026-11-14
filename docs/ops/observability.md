# Стек наблюдаемости (docker-compose.obs.yml)

Loki, Prometheus, Alertmanager (→ Telegram), Grafana, Tempo (трейсы), urgentry (журнал ошибок,
Sentry-совместимый; заменил GlitchTip), Caddy (reverse proxy + автоматический TLS), CrowdSec.
Целевой хост — VPS 2; сейчас стек запускается на той же машине, что и основной
`docker-compose.yml`.

## Запуск

```bash
# переменные OBS_*, GRAFANA_*, URGENTRY_*, TEMPO_*, ALERT_* — см. .env.example (секция OBSERVABILITY STACK)
make obs-config            # docker compose config
make obs-up                # make init-dirs + up -d --wait (все сервисы healthy)
make obs-ps / obs-logs [SERVICE=grafana] / obs-down

# тестовая копия (каталоги <PROJECT_DATA_DIR>_test/obs/*, порты +20000)
make env-test
make obs-up   ENV_FILE=.env.test
make obs-down ENV_FILE=.env.test
```

Имя compose-проекта: `${PROJECT_NAME}_obs` → dev `tg_shop_platform_obs`,
test `tg_shop_platform_test_obs`; dev и test работают одновременно.

## Сеть

Собственная сеть compose-проекта, без общей внешней сети с основным стеком. Все сервисы,
кроме Caddy, публикуются только на `127.0.0.1:${*_PORT_HOST}`. Приложение отправляет логи/
метрики через порты хоста (на VPS 2 — через VPN). Prometheus видит хост как
`host.docker.internal` (host-gateway) для скрейпа метрик приложения.
Caddy публикуется на `${CADDY_BIND_ADDRESS:-0.0.0.0}:80/443` и закрывает **всё** HTTP Basic Auth
(`OBS_AUTH_USER` / `OBS_AUTH_HASH`, без исключений; заголовок `Authorization` в Grafana/urgentry
не передаётся).

## Доступ через Caddy (`CADDY_MODE`)

| Режим | Адреса | TLS |
|---|---|---|
| `ip` (в `.env.example`) | `https://<VPS_IP>/` — urgentry (под-путь не поддерживается), `https://<VPS_IP>/grafana/` — Grafana | `tls internal` (самоподписанный: предупреждение браузера, `curl -k`) |
| `domain` | `https://<GRAFANA_DOMAIN>/`, `https://<URGENTRY_DOMAIN>/` (можно `grafana.<ip>.nip.io`) | Let's Encrypt, `*.localhost` — внутренний CA |

http → https редиректит сам Caddy, **но на стандартный порт 443**: при нестандартном
`CADDY_HTTPS_PORT_HOST` (test-стек: 20443) редирект с `http://host:<CADDY_HTTP_PORT_HOST>/` ведёт на
`https://host/` и не работает — открывайте сразу `https://host:<CADDY_HTTPS_PORT_HOST>/`. На
прод-портах 80/443 ограничения нет; ROOT_URL Grafana и `URGENTRY_DOMAIN` содержат порт и не
затронуты. Конфиги: `ops/observability/caddy/Caddyfile.ip` / `.domain`
(монтируется `Caddyfile.${CADDY_MODE}`). `GF_SERVER_ROOT_URL`, `GF_SERVER_SERVE_FROM_SUB_PATH` и
`URGENTRY_DOMAIN` вычисляются из `CADDY_MODE` скриптом `ops/scripts/obs_mode.sh` во всех целях
`make obs-*`; `make obs-up`/`obs-config` дополнительно проверяют `VPS_IP` (ip) или домены (domain)
и наличие `OBS_AUTH_*`. `CADDY_MODE` обязателен для всех obs-целей, кроме `obs-down`/`obs-ps`/
`obs-logs` (старый `.env` без него не переводится молча в режим `ip`). При прямом
`docker compose` действуют дефолты режима `ip`.

Prometheus скрейпит Grafana по пути, зависящему от режима: в `ip` (sub path) `/metrics` отвечает
301 на публичный ROOT_URL, поэтому compose монтирует
`ops/observability/prometheus/grafana_targets.${CADDY_MODE}.yml` (`__metrics_path__`
`/grafana/metrics` или `/metrics`). Healthcheck Grafana — `/api/health` (200 в обоих режимах).

### Обновление существующего .env

`.env`, созданный до Basic Auth: добавьте `CADDY_MODE` (`domain` — если доступ уже был по
`GRAFANA_DOMAIN`/`URGENTRY_DOMAIN`; `ip` + `VPS_IP`), `OBS_AUTH_USER=admin`, `OBS_AUTH_HASH=`,
затем `make obs-auth` и `make obs-up`. `SENTRY_DSN` с публичным хостом (`urgentry.localhost`,
домен) теперь отвергается при старте приложения — очистите его и выполните `make obs-bootstrap`.

Учётные данные: `make env` создаёт `OBS_AUTH_USER=admin` и случайный пароль (показывается один
раз; без docker — предупреждение), `make obs-auth` задаёт новый пароль (интерактивно; пустой ответ —
сгенерировать; `PASSWORD_STDIN=1` — из stdin) и пересоздаёт запущенный caddy. В `.env` хранится только
`OBS_AUTH_HASH` = base64 bcrypt-хеша (`$` в `.env` недопустим; `basic_auth` Caddy 2.10 принимает
base64-хеш напрямую). Вход: Basic Auth → затем логин Grafana/urgentry.

## Порты (dev / test)

| Сервис       | Переменная               | dev  | test  |
|--------------|--------------------------|------|-------|
| Grafana      | `GRAFANA_PORT_HOST`      | 3000 | 23000 |
| Loki         | `LOKI_PORT_HOST`         | 3100 | 23100 |
| Prometheus   | `PROMETHEUS_PORT_HOST`   | 9090 | 29090 |
| Alertmanager | `ALERTMANAGER_PORT_HOST` | 9093 | 29093 |
| urgentry UI  | `URGENTRY_PORT_HOST`     | 8000 | 28000 |
| urgentry ingest (Caddy :8081) | `URGENTRY_INGEST_PORT_HOST` | 8081 | 28081 |
| Tempo OTLP gRPC / HTTP | `TEMPO_OTLP_GRPC_PORT_HOST` / `TEMPO_OTLP_HTTP_PORT_HOST` | 4317 / 4318 | 24317 / 24318 |
| Tempo API    | `TEMPO_PORT_HOST`        | 3200 | 23200 |
| Caddy HTTP   | `CADDY_HTTP_PORT_HOST`   | 80   | 20080 |
`OBS_URGENTRY_DATA_DIR`, `OBS_TEMPO_DATA_DIR`, `OBS_GRAFANA_DATA_DIR`,
`OBS_PROMETHEUS_DATA_DIR`, `OBS_LOKI_DATA_DIR`, `OBS_ALERTMANAGER_DATA_DIR`,
`OBS_CADDY_DATA_DIR` — создаются `make init-dirs`.
Grafana/Prometheus/Loki/Alertmanager запущены с `user: APP_UID:APP_GID`, поэтому их
каталоги принадлежат пользователю хоста. Каталог urgentry принадлежит uid 10001 (пользователь
образа), Caddy стартует от root и сам выставляет владельца.
`PROJECT_DATA_DIR` должен быть доступен на запись `APP_UID` (см. `.env.example`).
каталоги принадлежат пользователю хоста (chown не нужен). PostgreSQL/Valkey/Caddy стартуют
от root и сами выставляют владельца своих каталогов (как postgres/redis в основном стеке).
`PROJECT_DATA_DIR` должен быть доступен на запись `APP_UID` (см. `.env.example`).

## Retention

Prometheus `--storage.tsdb.retention.time=30d` (`PROMETHEUS_RETENTION`),
Loki `retention_period: 720h` (compactor), Tempo `block_retention: 720h`,
Alertmanager silences/notifications `720h`.

## Доступ локально

Локально: `CADDY_MODE=ip`, `VPS_IP=127.0.0.1`.

- Grafana: `https://127.0.0.1/grafana/` через Caddy (Basic Auth, затем `GRAFANA_ADMIN_USER` /
  `GRAFANA_ADMIN_PASSWORD`) или напрямую http://127.0.0.1:3000/grafana/ (в режиме `domain` —
  http://127.0.0.1:3000/). Источники Prometheus, Loki, Alertmanager подключены через provisioning.
- urgentry UI: `https://127.0.0.1/` через Caddy (Basic Auth, затем `URGENTRY_BOOTSTRAP_EMAIL` /
  `URGENTRY_BOOTSTRAP_PASSWORD` из `.env`). urgentry под-путь не поддерживает, поэтому в режиме
  `ip` занимает корень `/`, Grafana — `/grafana/`.
- Проверка: `curl -k -u admin:<пароль Basic Auth> https://127.0.0.1/grafana/api/health`;
  без `-u` Caddy отвечает 401. urgentry: `/healthz` (healthcheck контейнера).
- API urgentry — на сервере по PAT (`Authorization: Bearer $URGENTRY_BOOTSTRAP_PAT`), мимо Caddy.


## urgentry (журнал ошибок) и bootstrap

urgentry v0.2.11 (Sentry-совместимый, лицензия FSL-1.1) заменил GlitchTip: один контейнер,
SQLite в `OBS_URGENTRY_DATA_DIR` (`/data`, владелец uid 10001), порт 8080, проверка `/healthz`.
Образ собирается из исходников: `ops/observability/urgentry/Dockerfile` (build-arg
`GOPROXY`/`GOSUMDB` — `URGENTRY_BUILD_GOPROXY`/`URGENTRY_BUILD_GOSUMDB`, базовые образы tag+digest).

| Переменная | Значение |
|---|---|
| `URGENTRY_BOOTSTRAP_EMAIL` | e-mail администратора |
| `URGENTRY_BOOTSTRAP_PASSWORD` | пусто → генерируется (hex) и записывается в `.env` |
| `URGENTRY_BOOTSTRAP_PAT` | personal access token (hex), пусто → генерируется; по нему obs-bootstrap читает ключ DSN через API |
| `URGENTRY_INTERNAL_URL` | адрес ingest из контейнеров приложения: `http://caddy:8081` (один хост) / `http://<VPN IP VPS 2>:8081` |
| `SENTRY_DSN` | вычисляется: `http://<key>@<хост URGENTRY_INTERNAL_URL>/1` |

- `make obs-bootstrap` (вызывается из `make obs-up`) дописывает пустые `URGENTRY_BOOTSTRAP_*` и
  `SENTRY_DSN` в `ENV_FILE` (копия `ENV_FILE.bak`), идемпотентно. Для `PROJECT_ENVIRONMENT=test`
  `SENTRY_DSN` не пишется.
- **DSN и числовой project id.** `sentry_sdk` требует числовой id проекта, urgentry работает со
  slug. Внутренний listener Caddy `:8081` переписывает `/api/1/*` → `/api/default-project/*`
  и проксирует в `urgentry:8080`; без Basic Auth и без access-log, публикуется только на
  `${OBS_INGEST_BIND_ADDRESS}:${URGENTRY_INGEST_PORT_HOST:-8081}`. Ограничение: id `1`
  жёстко привязан к одному проекту `default-project`.
- Публичный DSN запрещён: хост `SENTRY_DSN` (валидатор `ObservabilitySettings`) и
  `URGENTRY_INTERNAL_URL` (`ops/scripts/check_internal_url.sh`) — только приватный IP или имя
  Docker-сервиса без точки (`caddy`, `localhost`).
- Применить DSN: `make agent-up` (с `ALLOY_SINGLE_HOST=1` overlay
  `ops/observability/alloy/compose.single-host.yml` подключает ботов к сети obs-стека, где
  резолвится `caddy`).
- urgentry имеет недокументированный OTLP-эндпоинт (только JSON) — **не используется**: трейсы
  идут в Tempo (см. ниже).

## Трассировка: OpenTelemetry → Alloy → Tempo

Решение: ошибки — `sentry_sdk` → urgentry; трейсы — OTel SDK → Alloy → Tempo, без дублирования в
urgentry; в событии Sentry есть `contexts.trace` (trace_id из OTel).

- **Приложение** (`app/core/telemetry.py`): OTLP gRPC на `OTEL_EXPORTER_OTLP_ENDPOINT`
  (`http://alloy:4317`), `BatchSpanProcessor` (неблокирующий), `ScrubbingSpanExporter`
  маскирует секреты в атрибутах спанов (правила `app/core/observability/sentry.py`).
  SQLAlchemy инструментирован глобально; FastStream — `RabbitTelemetryMiddleware` (W3C
  `traceparent` в заголовках RabbitMQ; при выключенном OTel trace_id переносит
  `TraceIdFallbackMiddleware`); корневой span на апдейт Telegram — `AiogramTracingMiddleware`
  (своей OTel-инструментации у aiogram нет). Health-пробы — под `suppress_instrumentation`,
  aiohttp health не трассируется. structlog пишет `trace_id`/`span_id`. Sentry:
  `traces_sample_rate=None`, `trace_propagation_targets=[]`.
- **Переменные:** `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SDK_DISABLED`, `OTEL_SERVICE_NAME`
  (пусто — имя компонента), `OTEL_TRACES_SAMPLER_ARG` (доля 0..1; старые имена
  `OTEL_TRACES_SAMPLER_RATIO`/`OTEL_SAMPLE_RATIO` — алиасы). **Отключить трейсинг:**
  `OTEL_SDK_DISABLED=true` или пустой `OTEL_EXPORTER_OTLP_ENDPOINT` (логи сохраняют `trace_id`).
- **Alloy** (`ops/observability/alloy/config.alloy`): `otelcol.receiver.otlp` (4317/4318, только
  `expose`, наружу не публикуется) → `memory_limiter` → `batch` →
  `otelcol.exporter.otlp "tempo"` (`OBS_TEMPO_OTLP_ENDPOINT`: `tempo:4317` на одном хосте,
  `<VPN IP VPS 2>:4317` на двух). Пустой endpoint — экспортёр не стартует; `make agent-config`/
  `make agent-up` проверяют его заранее (`agent_tempo_guard`, rc≠0 с понятной ошибкой).
- **Tempo** `grafana/tempo:3.1.1@sha256:64e2fbd5…` (`TEMPO_IMAGE`), данные в
  `OBS_TEMPO_DATA_DIR`, retention `backend_worker.compaction.block_retention: 720h`, проверка
  конфига `-config.verify=true` в `make obs-validate`. Metrics-generator (span-metrics,
  service-graphs) → remote_write в Prometheus: `traces_spanmetrics_calls_total`,
  `traces_spanmetrics_latency_bucket`, `traces_service_graph_request_total`,
  `traces_service_graph_request_failed_total` (лейблы `service`, `span_name`, `span_kind`,
  `status_code`). Алерт `TempoDown`.
- **Grafana:** источник Tempo (uid `tempo`), Loki derivedFields по structured metadata `trace_id`
  (`matcherType: label`), tracesToLogsV2 (трейс → логи), дашборд `tracing-red` (RED по
  span-metrics). Панель Service map (`queryType: serviceMap`) Grafana строит на фронтенде
  запросами к Prometheus: через `/api/ds/query` она отдаёт 500 — проверять в браузере.
- Ограничения: сквозной трейс Telegram-апдейт → bot_worker пока не проверен (боты — веха II);
  A-NIT-8 отложен в веху II.
- Демо и смоук: `make otel-smoke` (см. `docs/reports/milestone-I-demo.md`).

## Алерты

Alertmanager → Telegram: `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` (целое число).
Если пусто — receiver `blackhole` (предупреждение в логе). `make env-test` всегда очищает эти
переменные. Токен пишется в файл `/tmp/telegram_bot_token` (0600) внутри контейнера и не
попадает в конфиг/argv. Правила — `ops/observability/prometheus/rules/*.yml`
(перезагрузка: `curl -X POST http://127.0.0.1:9090/-/reload`).

Правила `rules/platform.yml` (метрики — от Alloy, лейблы `env`, `host`):

| Алерт | Выражение | Severity |
|---|---|---|
| `ServiceDown` | `up{job=~"node\|postgres\|redis\|rabbitmq\|app"} == 0`, for 2m | critical |
| `AgentMetricsAbsent` | `absent(up{job="node"})`, for 5m (Alloy/remote write не работает) | critical |
| `PostgresDown` | `pg_up == 0`, for 1m | critical |
| `RedisDown` | `redis_up == 0`, for 1m | critical |
| `RabbitmqDown` | `up{job="rabbitmq"} == 0`, for 1m | critical |
| `PgPartmanBgwDown` | `pg_partman_maintenance_age_seconds > 2*3600+900` при `pg_up == 1`, for 10m | critical |
| `PgPartmanMetricsAbsent` | `absent(pg_partman_maintenance_age_seconds)` при `pg_up == 1`, for 15m | warning |
| `PgPartmanFuturePartitionsMissing` | `pg_partman_partitions_future_count < 2` или `pg_partman_partitions_horizon_seconds < 30d`, for 30m | critical |
| `PostgresStatActivityAbsent` | `absent(pg_stat_activity_count)` при `pg_up == 1`, for 10m | warning |
| `LogsDroppedIncreasing` | `increase(logs_dropped_total[5m]) > 0` по service/reason | warning |
| `LogsDroppedHigh` | `sum(rate(logs_dropped_total[10m])) > 1` | critical |
| `DiskSpaceLow` / `DiskSpaceCritical` | `avail/size < 0.15` (10m) / `< 0.05` (5m) для `/` и `/data` | warning / critical |

Метрики pg_partman — custom queries Alloy (`ops/observability/alloy/postgres_queries.yml`,
`custom_queries_config_path` у `prometheus.exporter.postgres`), лейбл `parent_table`:
`pg_partman_maintenance_age_seconds` (секунд с `part_config.maintenance_last_run`),
`pg_partman_maintenance_premake`, `pg_partman_partitions_future_count` (партиции с нижней
границей > now()), `pg_partman_partitions_horizon_seconds` (до верхней границы последней
партиции). BGW pg_partman 5.x **не виден в `pg_stat_activity`** (процесс «pg_partman master
background worker» без подключения к БД между запусками), поэтому его отказ определяется по
эффекту — `maintenance_last_run` перестаёт обновляться (интервал `pg_partman_bgw.interval=3600`).
Права роли `monitoring` (`USAGE ON SCHEMA partman`, `SELECT ON partman.part_config`) выдаёт
`ops/scripts/ensure_monitoring_role.py` (из `migrate.sh`, идемпотентно).

Проверка и тесты правил (тот же образ, что в obs-стеке):

```bash
IMG=prom/prometheus:v3.6.0
docker run --rm --entrypoint promtool -v "$PWD/ops/observability/prometheus:/p:ro" -w /p $IMG check rules rules/*.yml
docker run --rm --entrypoint promtool -v "$PWD/ops/observability/prometheus:/p:ro" -w /p/tests $IMG test rules rules_test.yml
curl -s -X POST http://127.0.0.1:9090/-/reload && curl -s http://127.0.0.1:9090/api/v1/rules  # health=ok
```

Отложено (follow-up):

- `AppHealthNotReady` — метрика готовности не экспортируется, blackbox exporter не развёрнут.
- `WorkerHeartbeatMissing` — heartbeat воркера публикуется с вехи II.

## Дашборды

Provisioning `grafana/provisioning/dashboards/dashboards.yml` → папка `tg_shop_platform`,
JSON в `ops/observability/grafana/dashboards/` (правки только в файлах, UI read-only):

- `tgsp-overview` «Platform overview» — up по job, firing alerts, logs dropped, ошибки из Loki,
  p95 `update_duration_seconds`, `active_bots`, диск, CPU;
- `tgsp-datastores` «PostgreSQL / Redis / RabbitMQ» — `pg_up`, xact commit/rollback, соединения
  по state, deadlocks, cache hit, `redis_connected_clients`, `rabbitmq_queues`/`rabbitmq_connections`;
- `tgsp-logs` «Logs» — объём логов по `service`, ошибки (`detected_level=~"error|critical|fatal"`),
  `logs_dropped_total` по reason, поток ошибок.

Ожидание порта в скриптах: `ops/scripts/wait_for_it.sh host:port [-t 30] [-q] [-- cmd ...]`
(POSIX sh; коды 0 — доступен, 1 — таймаут, 2 — ошибка аргументов).

## Образы

Закреплены tag@digest, переопределяются `*_IMAGE` (список — в `.env.example`).

## Агент сбора VPS 1 (Grafana Alloy)

Сервис `alloy` в `docker-compose.yml`, профиль `observability` (в `make up` не стартует).
Конфиг — `ops/observability/alloy/config.alloy`, образ
`grafana/alloy:v1.11.3@sha256:8c7256f4…` (`ALLOY_IMAGE`).

- **Логи:** `discovery.docker` (фильтр `com.docker.compose.project=${PROJECT_NAME}`) →
  `loki.source.docker` → разбор JSON: метки `service`, `container`, `compose_project`,
  `level`, `logger`; `trace_id` — structured metadata (не метка) → `loki.write`
  (`OBS_LOKI_PUSH_URL`).
- **Метрики:** `prometheus.exporter.unix` (job `node`), `prometheus.exporter.postgres`
  (job `postgres`, роль `POSTGRES_MONITORING_USER`), `prometheus.exporter.redis` (job `redis`),
  RabbitMQ `rabbitmq:15692` (плагин `rabbitmq_prometheus` включён в образе
  `rabbitmq:4.3-management`, job `rabbitmq`), приложение `master_bot:8080/metrics`,
  `bot_worker:8080/metrics` (job `app`, метка `service`) → `prometheus.remote_write`
  (`OBS_PROMETHEUS_REMOTE_WRITE_URL`). Prometheus obs-стека запущен с
  `--web.enable-remote-write-receiver`.
- Общие метки всех рядов/потоков: `env` (`PROJECT_ENVIRONMENT`), `host` (`ALLOY_HOST_LABEL`).
- Ротация Docker-логов: `x-logging` (json-file, `DOCKER_LOG_MAX_SIZE=20m`,
  `DOCKER_LOG_MAX_FILE=5`) во всех сервисах обоих compose-файлов.
- Healthcheck `master_bot`/`bot_worker`: `GET /health/live` на `OBSERVABILITY_HTTP_PORT`
  через `python -c urllib.request` (порт наружу не публикуется).

### Docker API через docker-socket-proxy

`docker.sock` в Alloy **не монтируется**. Alloy (`discovery.docker`, `loki.source.docker`)
ходит в Docker API по `DOCKER_HOST_URL` (по умолчанию `tcp://docker-socket-proxy:2375`) через
сервис `docker-socket-proxy` (профиль `observability`, образ
`tecnativa/docker-socket-proxy:v0.4.1@sha256:3400c429…`, переопределяется
`DOCKER_SOCKET_PROXY_IMAGE`). Proxy (HAProxy) сам монтирует `/var/run/docker.sock` (`ro`),
портов не публикует и доступен только из internal-сети `docker_api` (alloy + proxy, без выхода
наружу). Healthcheck — `wget http://127.0.0.1:2375/version` (busybox в образе), Alloy ждёт
`service_healthy`. Логи — общий якорь `x-logging`.

Разрешено (только GET/HEAD, `POST=0`):

| Переменная | Эндпоинты | Зачем |
|---|---|---|
| `CONTAINERS=1` | `/containers/json`, `/containers/{id}/json`, `/containers/{id}/logs` | discovery контейнеров и чтение их логов |
| `NETWORKS=1` | `/networks` | `discovery.docker` вычисляет сетевые метки; без неё — 403 и `Unable to refresh target groups` (проверено) |
| `EVENTS=1` | `/events` | отслеживание старта/остановки контейнеров |
| `VERSION=1`, `PING=1` | `/version`, `/_ping` | согласование версии API клиентом Docker (`HEAD /_ping`, `GET /version` в логах proxy) и healthcheck |

Всё остальное (`INFO`, `IMAGES`, `EXEC`, `POST`, `ALLOW_START/STOP/RESTARTS`, …) = 0 → 403.

> **Остаточный риск.** Компрометация Alloy даёт чтение метаданных контейнеров (в т. ч.
> переменных окружения из `/containers/{id}/json`), сетей и логов контейнеров, но не
> управление хостом: создание/запуск/exec/удаление контейнеров отклоняются proxy (403).
> Компрометация самого proxy по-прежнему равна доступу к `docker.sock`.

### Переменные

| Переменная | Назначение |
|---|---|
| `ALLOY_DATA_DIR` | WAL/позиции Alloy (bind, `make init-dirs`), test: `<PROJECT_DATA_DIR>_test/alloy` |
| `ALLOY_PORT_HOST` | UI Alloy на `127.0.0.1` (12345, test 32345) |
| `ALLOY_HOST_LABEL` | метка `host` |
| `ALLOY_SINGLE_HOST` | `1` — obs-стек на этом же хосте (см. ниже), `0` — прод |
| `DOCKER_SOCKET_PROXY_IMAGE` | образ docker-socket-proxy (tag+digest) |
| `DOCKER_HOST_URL` | адрес Docker API для Alloy (по умолчанию `tcp://docker-socket-proxy:2375`) |
| `OBS_LOKI_PUSH_URL` | `http://<loki>:3100/loki/api/v1/push` |
| `OBS_PROMETHEUS_REMOTE_WRITE_URL` | `http://<prometheus>:9090/api/v1/write` |
| `POSTGRES_MONITORING_USER/PASSWORD` | read-only роль (`pg_monitor`) |
| `OBS_INGEST_BIND_ADDRESS` | (VPS 2) адрес публикации портов Loki/Prometheus |

### Роль мониторинга PostgreSQL

Создаётся/обновляется в `db_update` шагом `ops/scripts/ensure_monitoring_role.py` после
`alembic upgrade head` (роль кластерная, а миграции — по БД и без runtime-секретов).
Идемпотентно: `CREATE ROLE` / `ALTER ROLE` + `GRANT pg_monitor`, `CONNECT`,
`default_transaction_read_only=on`, `CONNECTION LIMIT 5`. Пароль передаётся серверу как
SCRAM-SHA-256 verifier (в логи PostgreSQL и `db_update` открытый пароль не попадает).
Пароль обязателен для Alloy (hex, `openssl rand -hex 24`). Пустой
`POSTGRES_MONITORING_PASSWORD` — шаг пропускается с предупреждением; плейсхолдер
(`change_me` и т. п.) — ошибка конфигурации (код 2, `db_update` падает). Порядок
`db_update`: `alembic upgrade` → platform setup → роль мониторинга → sync retention
pg_partman. Смена пароля: изменить `.env`,
`make migrate`, затем перезапустить `alloy`.

### Запуск на одном хосте (dev/test)

Порты obs-стека опубликованы только на `127.0.0.1` хоста, а `host.docker.internal`
(host-gateway) из контейнера ведёт на адрес `docker0` — туда они не опубликованы
(проверено: connection refused). Поэтому при `ALLOY_SINGLE_HOST=1` `make agent-*` добавляет
`ops/observability/alloy/compose.single-host.yml`: Alloy подключается ко внешней сети
`${PROJECT_NAME}_obs_default`, URL — `http://loki:3100/...`, `http://prometheus:9090/...`.
Obs-стек должен быть уже запущен (`make obs-up`).

```bash
make obs-up          # если ещё не запущен
make agent-up        # основной стек + alloy, --wait (боты healthy)
make agent-logs / agent-down   # agent-down останавливает только alloy
```

### Перенос на реальные VPS 1 / VPS 2

- VPS 2 (`.env`): `OBS_INGEST_BIND_ADDRESS=<VPN-IP VPS 2>` — Loki/Prometheus слушают
  только VPN-интерфейс (никогда не `0.0.0.0`); фаервол — только с VPN-IP VPS 1.
- VPS 1 (`.env`): `ALLOY_SINGLE_HOST=0`,
  `OBS_LOKI_PUSH_URL=http://<VPN-IP VPS 2>:3100/loki/api/v1/push`,
  `OBS_PROMETHEUS_REMOTE_WRITE_URL=http://<VPN-IP VPS 2>:9090/api/v1/write`,
  `ALLOY_HOST_LABEL=vps1`, `PROJECT_ENVIRONMENT=prod`; затем `make agent-up`.
- Трафик идёт внутри VPN (WireGuard), поэтому без TLS/basic-auth; при отказе от VPN
  нужен reverse proxy с TLS и аутентификацией.

### Проверочные запросы

```bash
P=http://127.0.0.1:9090/api/v1/query
curl -sG $P --data-urlencode 'query=up{env!=""}'   # node, postgres, redis, rabbitmq, app x2 = 1
curl -sG $P --data-urlencode 'query=pg_up'
curl -sG $P --data-urlencode 'query=redis_up'
curl -sG $P --data-urlencode 'query=count(rabbitmq_build_info)'
curl -sG http://127.0.0.1:3100/loki/api/v1/query_range \
  --data-urlencode 'query=sum by (service) (count_over_time({compose_project="tg_shop_platform"}[10m]))' \
  --data-urlencode "start=$(( $(date +%s) - 600 ))000000000" --data-urlencode step=600
```

Известные ограничения: collector `diskstats` пишет в лог ошибку про `/run/udev/data` (нет udev в
контейнере — метрики дисков собираются без udev-свойств).

## Защита от сканеров и брутфорса

- **Catch-all.** В обоих режимах `http:// { abort }` и `https:// { abort }`: запрос с чужим `Host` (или HTTP/1.0 без Host) получает сброс соединения (curl exit 52). Свои адреса (`VPS_IP` в ip-режиме, домены в domain-режиме) по HTTP получают 308 → HTTPS, ACME HTTP-01 работает. В ip-режиме TLS-рукопожатие с любым SNI получает сертификат `VPS_IP` (`default_sni`), после чего запрос с чужим Host сбрасывается; в domain-режиме неизвестный SNI вообще не получает сертификата (TLS-ошибка, curl exit 35) — такие попытки в access.log не попадают.
- **Access-log** (JSON, `OBS_CADDY_LOGS_DIR/access.log`, ротация 50 MiB × 5, права 0640): ровно одна строка на каждый HTTP-запрос — на свои сайты и на оба catch-all; сброшенные запросы — со `status 0`. Файл заранее создаёт `make init-dirs`: файловый источник CrowdSec не подхватывает файл, появившийся после его старта. Runtime-логи Caddy по-прежнему идут в stdout (Alloy).
- **CrowdSec** (сервис `crowdsec`, `crowdsecurity/crowdsec:v1.8.2`, переопределяется `CROWDSEC_IMAGE`) читает только этот файл (ro), доступа к Docker нет. Коллекция `crowdsecurity/caddy` = парсер caddy + `base-http-scenarios` + `http-cve`. Подбор пароля Basic Auth ловит `crowdsecurity/http-generic-bf`: парсер помечает 401 с `WWW-Authenticate: Basic` как `auth_fail`, бан после 6 неудач (5 + 1 за 10 с). Сброшенные catch-all запросы (`status 0`) разбираются парсером и учитываются сценариями по пути/User-Agent (sensitive-files, CVE, backdoors, bad-user-agent…), но не сценариями по кодам ответа (`http-probing` считает 403/404). Бан — 4 часа (профиль образа по умолчанию). Community blocklist (CAPI) подключается автоматически, Console (`cscli console enroll`) — опционально.
- **Первый старт CrowdSec требует сети**: образ скачивает hub (коллекции, парсеры) — без доступа в интернет контейнер не становится healthy.
- **Bouncer** встроен в Caddy (своя сборка: Caddy 2.10.2 + `caddy-crowdsec-bouncer/http` v0.13.0), опрашивает LAPI `http://crowdsec:8080` раз в 15 с; забаненный IP получает 403 ещё до запроса пароля. Bouncer **fail-open**: caddy стартует после запуска crowdsec (`service_started`), а при недоступном CrowdSec продолжает обслуживать запросы (Basic Auth остаётся), баны временно не применяются. Это видно алертами: `CrowdsecDown` (`up{job="crowdsec"} == 0` 5 мин) и `CrowdsecBouncerNoPulls` (CrowdSec жив, но bouncer ~10 мин не забирает решения — например, после смены ключа). Метрики CrowdSec (:6060) скрейпит Prometheus внутри obs-сети, на хост порт не публикуется (как и LAPI 8080).
- **Whitelist**: `crowdsecurity/whitelists` (127/8, ::1, 10/8, 172.16/12, 192.168/16) + `ops/observability/crowdsec/parsers/s02-enrich/tgsp-whitelist.yaml` (VPN 10.10.0.0/24, Tailscale 100.64.0.0/10, Docker, fd00::/8). Переменные окружения в парсерах CrowdSec не подставляются — другой диапазон VPN правится в файле, затем `docker compose ... restart crowdsec`.
- **Какой IP видит Caddy** (проверено на стенде): запросы с самого хоста на 127.0.0.1 и с адреса хоста из других docker-сетей приходят через docker-proxy с IP шлюза obs-сети (`172.x.0.1`, whitelist) — их CrowdSec не банит. Внешние клиенты (iptables DNAT) видны с реальным IP.
- **IPv6**: не задавайте `CADDY_BIND_ADDRESS=::` при выключенном IPv6 в Docker — IPv6-клиенты пойдут через docker-proxy и будут видны как IP шлюза (whitelist), то есть CrowdSec их не забанит. Оставляйте `0.0.0.0` или включите IPv6 в Docker (`ip6tables`).
- **Баны**: `make obs-bans` — список решений; `make obs-bans UNBAN=<ip>` — снять бан (значение проверяется как IP). Не баньте вручную IP шлюза Docker (`172.x.0.1`): `cscli decisions add` применяется и к адресам из whitelist, и это отрежет весь локальный доступ.
- **Аргументы make**: GNU make раскрывает аргументы (`X=...`) до запуска цели — `$(shell …)` в значении выполнится. Не вставляйте в аргументы make непроверенный текст (например, IP из логов — сначала проверьте его). `UNBAN` дополнительно защищён (`unexport` + проверка IP).
- **Ресурсы**: crowdsec `mem_limit: 256m`.

### Ротация ключа bouncer

Образ регистрирует bouncer `caddy` из `BOUNCER_KEY_caddy` только если его ещё нет (`/docker_start.sh`, `register_bouncer`). После смены `CROWDSEC_BOUNCER_KEY` при сохранённых данных LAPI отвечает bouncer'у 403, баны молча не применяются (алерт `CrowdsecBouncerNoPulls`). Процедура: изменить ключ в `.env` (`openssl rand -hex 32`) → `make obs-crowdsec-rekey` (поднимает crowdsec, удаляет bouncer `caddy`, если он зарегистрирован, затем пересоздаёт crowdsec — регистрация с новым ключом — и caddy; ошибка удаления прерывает цель) → `docker compose ... exec crowdsec cscli bouncers list` показывает свежий `Last API pull`.

### Проверка защиты на проде (с внешнего IP, не из whitelist)

```bash
curl -sv -H 'Host: x' http://<VPS_IP>/            # сброс соединения (curl exit 52)
curl -skv -H 'Host: x' https://<VPS_IP>/          # сброс после TLS (ip-режим)
for i in $(seq 6); do curl -sk -o /dev/null -w '%{http_code}\n' -u admin:wrong https://<VPS_IP>/; done
sleep 20; curl -sk -o /dev/null -w '%{http_code}\n' https://<VPS_IP>/   # 403 — IP забанен
make obs-bans                                      # на VPS 2: решение с вашим IP
make obs-bans UNBAN=<ваш IP>                       # доступ восстановлен
```

### Сборка образа Caddy и зеркала Go

Образ Caddy собирается локально (`make obs-up` / `make obs-validate`, `ops/observability/caddy/Dockerfile`): xcaddy скачивает Go-модули. `proxy.golang.org` отдаёт архивы через `storage.googleapis.com`, который в части регионов отвечает `403 ... not available in your location` (так же `google.golang.org/api`), поэтому по умолчанию `GOPROXY="https://goproxy.cn|https://proxy.golang.org|direct"` — первым стоит зеркало goproxy.cn (proxy.golang.org в части регионов зависает/403 на storage.googleapis.com; goproxy.io отдаёт zip с 403). Контрольные суммы проверяются по `sum.golang.org` (Go берёт sumdb через `/sumdb/` прокси), так что зеркало не может подменить модуль. Другое зеркало — `CADDY_BUILD_GOPROXY` в `.env` (в кавычках); `GOSUMDB=off` не использовать.

Проверка без запуска стека: `make obs-validate` (сборка образа + `caddy validate` для `Caddyfile.<CADDY_MODE>`; монтируется только Caddyfile). `make obs-config` остаётся быстрым (только `docker compose config` и проверка переменных).

### Обновление существующего .env (CrowdSec)

Добавьте переменные **до** любых obs-команд: со старым `.env` падают даже `make obs-down` / `obs-ps` / `obs-logs` / `obs-bans` (compose проверяет обязательные переменные `:?`). Добавить в `.env`: `CROWDSEC_BOUNCER_KEY=<вывод openssl rand -hex 32>`, `OBS_CADDY_LOGS_DIR="${PROJECT_DATA_DIR}/obs/caddy-logs"`, `OBS_CROWDSEC_DATA_DIR="${PROJECT_DATA_DIR}/obs/crowdsec"`. `make env FORCE=1` — только для новой установки: он перегенерирует все секреты. Переменная `CADDY_IMAGE` больше не используется (образ Caddy собирается локально; для `make obs-auth` — `CADDY_AUTH_IMAGE`). Затем `make init-dirs && make obs-up`.
