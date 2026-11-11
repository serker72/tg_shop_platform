# Стек наблюдаемости (docker-compose.obs.yml)

Loki, Prometheus, Alertmanager (→ Telegram), Grafana, GlitchTip (web + worker + migrate,
собственные PostgreSQL и Valkey), Caddy (reverse proxy + автоматический TLS).
Целевой хост — VPS 2; сейчас стек запускается на той же машине, что и основной
`docker-compose.yml`.

## Запуск

```bash
# переменные OBS_*, GRAFANA_*, GLITCHTIP_*, ALERT_* — см. .env.example (секция OBSERVABILITY STACK)
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
Caddy публикуется на `${CADDY_BIND_ADDRESS:-0.0.0.0}:80/443`.

## Порты (dev / test)

| Сервис       | Переменная               | dev  | test  |
|--------------|--------------------------|------|-------|
| Grafana      | `GRAFANA_PORT_HOST`      | 3000 | 23000 |
| Loki         | `LOKI_PORT_HOST`         | 3100 | 23100 |
| Prometheus   | `PROMETHEUS_PORT_HOST`   | 9090 | 29090 |
| Alertmanager | `ALERTMANAGER_PORT_HOST` | 9093 | 29093 |
| GlitchTip    | `GLITCHTIP_PORT_HOST`    | 8000 | 28000 |
| Caddy HTTP   | `CADDY_HTTP_PORT_HOST`   | 80   | 20080 |
| Caddy HTTPS  | `CADDY_HTTPS_PORT_HOST`  | 443  | 20443 |

## Каталоги данных (только bind)

`OBS_POSTGRES_DATA_DIR`, `OBS_REDIS_DATA_DIR`, `OBS_GRAFANA_DATA_DIR`,
`OBS_PROMETHEUS_DATA_DIR`, `OBS_LOKI_DATA_DIR`, `OBS_ALERTMANAGER_DATA_DIR`,
`OBS_CADDY_DATA_DIR` — создаются `make init-dirs`.
Grafana/Prometheus/Loki/Alertmanager запущены с `user: APP_UID:APP_GID`, поэтому их
каталоги принадлежат пользователю хоста (chown не нужен). PostgreSQL/Valkey/Caddy стартуют
от root и сами выставляют владельца своих каталогов (как postgres/redis в основном стеке).
`PROJECT_DATA_DIR` должен быть доступен на запись `APP_UID` (см. `.env.example`).

## Retention

Prometheus `--storage.tsdb.retention.time=30d` (`PROMETHEUS_RETENTION`),
Loki `retention_period: 720h` (compactor), GlitchTip `GLITCHTIP_MAX_EVENT_LIFE_DAYS=30`,
Alertmanager silences/notifications `720h`.

## Доступ локально

- Grafana: http://127.0.0.1:3000 (логин `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`)
  или через Caddy: `https://grafana.localhost` (внутренний CA Caddy → `curl -k`/исключение
  в браузере). Источники Prometheus, Loki, Alertmanager подключены через provisioning.
- GlitchTip: http://127.0.0.1:8000 или `https://glitchtip.localhost`. Регистрация выключена;
  первый админ:
  `docker compose -f docker-compose.obs.yml --env-file .env exec glitchtip-web ./manage.py createsuperuser`
- Проверка: `curl -u admin:$GRAFANA_ADMIN_PASSWORD http://127.0.0.1:3000/api/datasources`,
  `curl http://127.0.0.1:8000/_health/`.

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

> **Принятый риск: `docker.sock` в Alloy.** Сокет `/var/run/docker.sock` смонтирован в
> контейнер `alloy` с `read_only: true`, но это ограничивает только файловую операцию над
> сокетом, а не Docker API: через сокет доступны все вызовы (создание контейнеров,
> монтирование `/` хоста и т. п.), то есть компрометация Alloy фактически даёт root-доступ
> к хосту. Риск принят ради сбора логов контейнеров (`discovery.docker`,
> `loki.source.docker`). **Follow-up:** поставить `docker-socket-proxy` (например,
> `tecnativa/docker-socket-proxy`) с доступом только на чтение `containers`/`logs`
> (`CONTAINERS=1`, `POST=0`, остальное запрещено) и направить Alloy на него по TCP.

### Переменные

| Переменная | Назначение |
|---|---|
| `ALLOY_DATA_DIR` | WAL/позиции Alloy (bind, `make init-dirs`), test: `<PROJECT_DATA_DIR>_test/alloy` |
| `ALLOY_PORT_HOST` | UI Alloy на `127.0.0.1` (12345, test 32345) |
| `ALLOY_HOST_LABEL` | метка `host` |
| `ALLOY_SINGLE_HOST` | `1` — obs-стек на этом же хосте (см. ниже), `0` — прод |
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
