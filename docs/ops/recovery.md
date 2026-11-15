# Восстановление платформы (recovery)

Процедура по ТЗ, раздел 34:

> Чистый сервер → развернуть Docker → восстановить PostgreSQL → восстановить MinIO →
> применить migrations/config → запустить сервисы → проверить health

Объём Этапа 2 — **процедура и smoke-тест**. Полноценный backup (cron `pg_dump`, выгрузка
копий в S3/на другой сервер, offline-копии, «умный» backup каталогов данных) — Этап 8 /
веха IV. Ниже предполагается, что копии уже есть:

- дамп PostgreSQL в custom-формате: `pg_dump -Fc` (файл `platform.dump`);
- копия бакетов MinIO (каталог, снятый `mc mirror`);
- `.env` (хранится отдельно от репозитория, в менеджере секретов/зашифрованном хранилище).

Секреты в этом документе не указываются: все пароли и ключи берутся из `.env`.

## 0. Smoke-тест процедуры

`tests/integration/test_recovery_smoke.py::test_restore_empty_db_and_object` на тестовом
стеке проверяет «скелет» восстановления: пустая scratch-БД (`*_test`) →
`alembic upgrade head` → platform setup → проверка `alembic_version`=head,
`system_settings` (id=1), сид языков и RBAC, `partman.part_config` обеих таблиц логов;
затем запись объекта в MinIO через `StorageService`, потеря локальной копии, чтение
и сверка sha256. Запуск:

```bash
make test-integration                     # весь набор, тест входит в него
# только этот тест — в окружении make test-integration (стек уже поднят):
set -a; . ./.env.test; set +a
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=$POSTGRES_PORT_HOST \
REDIS_HOST=127.0.0.1 REDIS_PORT=$REDIS_PORT_HOST \
RABBITMQ_HOST=127.0.0.1 RABBITMQ_PORT=$RABBITMQ_PORT_HOST \
MINIO_ENDPOINT=127.0.0.1:$MINIO_API_PORT_HOST REQUIRE_INFRA=1 \
uv run pytest -m integration tests/integration/test_recovery_smoke.py
```

Рекомендуется периодически (и после каждого изменения миграций) выполнять полный
прогон восстановления из реальной копии на отдельной машине.

## 1. Чистый сервер → Docker

```bash
# Docker Engine + compose plugin (официальная инструкция docs.docker.com/engine/install)
sudo apt-get update && sudo apt-get install -y ca-certificates curl git make chrony
# ... установка docker-ce docker-ce-cli containerd.io docker-compose-plugin по инструкции
sudo systemctl enable --now docker chrony

git clone <repo> tg_shop_platform && cd tg_shop_platform
git checkout <тег/коммит, из которого сделан backup>
cp /secure/location/.env .env           # восстановленный .env, права 600
make init-dirs                          # каталоги *_DATA_DIR, LOGS_DATA_DIR (+ system/tmp)
```

Владелец каталогов данных — `APP_UID:APP_GID` из `.env` (при ошибке `init-dirs` выводит
готовую команду `chown`). Сеть/VPN/firewall/NTP — см. [network.md](network.md).

## 2. Восстановление PostgreSQL

Запускается **только** контейнер `postgres` (приложение и `db_update` не стартуют, чтобы
не создать схему поверх пустой БД):

```bash
docker compose --env-file .env up -d --wait postgres
set -a; . ./.env; set +a
```

Образ `ops/postgres/Dockerfile` уже содержит pg_partman; `shared_preload_libraries=pg_partman_bgw`
и `pg_partman_bgw.dbname=${POSTGRES_DB}` заданы в `docker-compose.yml`. БД `${POSTGRES_DB}`
создаётся `initdb` при первом старте на пустом `POSTGRES_DATA_DIR` и должна быть пустой.

```bash
# дамп копируется в контейнер, восстановление от владельца БД
docker compose --env-file .env cp platform.dump postgres:/tmp/platform.dump
docker compose --env-file .env exec -T postgres \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error /tmp/platform.dump
docker compose --env-file .env exec -T postgres rm -f /tmp/platform.dump
```

Плановый дамп (`pg_dump -Fc`) включает схемы `partman` и `archive`, `CREATE EXTENSION
pg_partman SCHEMA partman`, `partman.part_config`, все партиции `user_activity` /
`audit_logs` (включая `*_default`) и `alembic_version`. Проверки после восстановления:

```bash
docker compose --env-file .env exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_partman';
   SELECT parent_table, premake, retention FROM partman.part_config ORDER BY 1;
   SELECT version_num FROM alembic_version;
   SELECT count(*) FROM system_settings;"
# фоновый воркер pg_partman (BGW) работает в этой БД:
docker compose --env-file .env exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT pid, backend_type FROM pg_stat_activity WHERE backend_type LIKE 'pg_partman%';"
```

Нюансы pg_partman:

- если дамп снят как plain SQL (`pg_dump -Fp`), восстанавливать через
  `psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < platform.sql`;
- при восстановлении в БД с **другим** именем BGW её не обслуживает — имя должно совпадать с
  `POSTGRES_DB` (иначе поправить `.env` и перезапустить `postgres`);
- партиции на текущий/будущие месяцы досоздаются: BGW (интервал 3600 с) или вручную
  `CALL partman.run_maintenance_proc();` сразу после восстановления;
- если в дампе нет партиций новых месяцев, строки до maintenance попадают в `*_default` —
  это штатно, maintenance их не теряет.

## 3. Восстановление MinIO

```bash
docker compose --env-file .env up -d --wait minio
# mc в отдельном контейнере в сети compose-проекта; ключи — из .env, не в истории shell
docker run --rm --network "${PROJECT_NAME}_default" \
  -e MC_HOST_dst="http://${MINIO_ACCESS_KEY}:${MINIO_SECRET_KEY}@minio:9000" \
  -e BUCKET="${MINIO_BUCKET}" \
  -v /backup/minio:/backup:ro minio/mc \
  sh -c 'mc mb --ignore-existing "dst/$BUCKET" && mc mirror --overwrite "/backup/$BUCKET" "dst/$BUCKET"'
```

- Имя сети — `docker network ls` (по умолчанию `<project>_default`); ключи —
  `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` из `.env`.
- Восстанавливаются все бакеты: основной (`STORAGE_BUCKET`, пусто = `MINIO_BUCKET`) и
  бакет архива аудита `MINIO_AUDIT_ARCHIVE_BUCKET` (если задан) — та же команда с другим `BUCKET`.
- Проверка: `mc ls --recursive dst/<bucket> | wc -l` и `mc du dst/<bucket>` сравнить с
  отчётом, сохранённым при backup.
- Альтернатива для холодного восстановления: остановить `minio` и вернуть каталог
  `MINIO_DATA_DIR` целиком (rsync из копии каталога), затем `make init-dirs`/права.
- На пустом MinIO бакеты создаёт одноразовый сервис `minio_init` (образ `minio` с `mc`,
  `mc mb --ignore-existing`): основной (`STORAGE_BUCKET`, пусто = `MINIO_BUCKET`) и
  `MINIO_AUDIT_ARCHIVE_BUCKET`, если задан и отличается. Идемпотентен; `db_update`,
  `master_bot`, `bot_worker` стартуют только после его успешного завершения. При
  `STORAGE_BACKEND=s3` ничего не делает (бакеты S3 создаются вне платформы). Ручной запуск:
  `docker compose --env-file .env up minio_init` (код выхода 0).

## 4. Migrations / config

```bash
make migrate     # init-dirs + db_update: wait_for_db → alembic upgrade head → setup → sync retention
```

`db_update` идемпотентен: на восстановленной БД применяет только миграции новее дампа,
`system_settings` не перезаписывает, синхронизирует retention pg_partman с настройками.

## 5. Запуск

```bash
# один хост (ALLOY_SINGLE_HOST=1): строго в этом порядке
make init-dirs
make obs-up                              # стек наблюдаемости, сеть <PROJECT_NAME>_obs_default, SENTRY_DSN
make agent-up                            # основной стек + Alloy + make ready
docker compose --env-file .env ps        # все сервисы running/healthy
```

Два сервера (VPS 1 + VPS 2): VPS 2 — `make init-dirs && make obs-up`, VPS 1 —
`make init-dirs && make agent-up`; подробно — [README](../../README.md#сценарий-2--vps-1-приложение--vps-2-мониторинг).

## 6. Проверка health

`make up` и `make agent-up` после `docker compose up --wait` (healthcheck = `/health/live`)
вызывают `make ready`: опрос `GET /health/ready` внутри `master_bot` и `bot_worker`
(`docker compose exec`, порт `OBSERVABILITY_HTTP_PORT`) каждые 2 с до `READY_TIMEOUT`
(по умолчанию 60 с). Оба 200 → `ready`; иначе печатается JSON `/health/ready` неготовых
сервисов и код выхода 1. Отдельно: `make ready [READY_TIMEOUT=120]`.

```bash
curl -fsS http://127.0.0.1:<порт сервиса>/health/live
curl -fsS http://127.0.0.1:<порт сервиса>/health/ready   # PostgreSQL, Redis, RabbitMQ, MinIO, heartbeat
```

Порт — `OBSERVABILITY_HTTP_PORT` (по умолчанию 8080) в контейнере сервиса; снаружи
контейнера проверять через опубликованный на `127.0.0.1`/VPN-IP порт или
`docker compose exec <service> ...`. Затем в Grafana (через Caddy либо SSH-туннель на
`127.0.0.1:3000`): Prometheus targets в состоянии `UP`, свежие логи в Loki, нет активных
алертов в Alertmanager. Ложные fail heartbeat после переезда — первым делом проверить
синхронизацию времени (см. [network.md](network.md#синхронизация-времени)).

## Чек-лист

- [ ] Docker, chrony, репозиторий на нужном коммите, `.env` восстановлен, `make init-dirs`
- [ ] PostgreSQL: `pg_restore` без ошибок, `pg_partman` + `part_config` + BGW на месте
- [ ] MinIO: все бакеты, количество/объём объектов совпадают
- [ ] `make migrate` успешно
- [ ] `make up` / `make obs-up`, все сервисы healthy
- [ ] `/health/ready` = ok, Grafana/Prometheus/Loki в порядке
