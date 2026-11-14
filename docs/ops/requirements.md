# Требования к серверам (VPS)

- **VPS 1** — основной стек (`docker-compose.yml`): приложение (`master_bot`, `bot_worker`),
  PostgreSQL 17, RabbitMQ, Redis, MinIO, агент Alloy.
- **VPS 2** — стек наблюдаемости (`docker-compose.obs.yml`): urgentry, Tempo, Loki, Prometheus,
  Alertmanager, Grafana, Caddy, CrowdSec. Сеть между серверами — [network.md](network.md),
  наблюдаемость — [observability.md](observability.md).

## Основание и оговорка

Цифры ниже — **оценка**, а не результат нагрузочного тестирования. Основа — замер dev-стека
в простое (2026-11-14; боты — заглушки, трафика нет). Нагрузочного тестирования не было.

Из количественных требований в ТЗ есть только «100+ ботов»
(`docs/technical_specification_telegram_shop_platform_revision_3.txt`, строка 1327:
«Как будет реализован запуск 100+ ботов?»). RPS и число пользователей в ТЗ не заданы.

Значения уточняются после вехи II (реальные боты) и нагрузочного теста.

## Замер в простое

`docker stats --no-stream`, 2026-11-14.

| VPS 1 | RAM, MiB | | VPS 2 | RAM, MiB |
|---|---:|---|---|---:|
| `postgres` | 68.6 | | `urgentry` | 29.5 |
| `redis` | 32.1 | | `tempo` | 54.4 |
| `rabbitmq` | 163.6 | | `loki` | 171.4 |
| `minio` | 143.6 | | `prometheus` | 177.1 |
| `master_bot` | 137.7 | | `alertmanager` | 50.7 |
| `bot_worker` | 132.3 | | `grafana` | 323.0 |
| `alloy` | 150.2 | | `caddy` | 33.6 |
| `docker-socket-proxy` | 28.5 | | `crowdsec` | 219.7 |
| **Итого** | **≈857** | | **Итого** | **≈1059** |

Образы: VPS 1 ≈1.9 GB, VPS 2 ≈1.5 GB.

## Сводная таблица

| Сервер | vCPU | RAM | Диск |
|---|---|---|---|
| Staging (всё на одном VPS) | 2 / 4 | 4 ГБ + swap 2 ГБ / 8 ГБ | 50 / 60 ГБ SSD |
| Production VPS 1 | 4 / 8 | 8 / 16 ГБ | 80 / 150+ ГБ NVMe |
| Production VPS 2 | 2 / 4 | 2 ГБ + swap 2 ГБ / 4 ГБ | 30 / 80+ ГБ SSD |

Staging — минимум / рекомендуемо; production — старт / рост.

## Staging (всё на одном VPS)

| Ресурс | Минимум | Рекомендуемо |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 4 ГБ + swap 2 ГБ | 8 ГБ |
| Диск | 50 ГБ SSD | 60 ГБ |

- На стенде собираются образы. Сборка urgentry из исходников на Go и сборка приложения
  повышают пик RAM.
- Build cache и образы занимают десятки ГБ — периодически выполняйте `docker builder prune`.
- Тестовый стек `make test-integration` добавляет ≈0.3 ГБ RAM.

## Production VPS 1

App, PostgreSQL 17, RabbitMQ, Redis, MinIO, Alloy.

| Ресурс | Старт | Рост |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 8 ГБ | 16 ГБ |
| Диск | 80 ГБ NVMe, расширяемый | 150+ ГБ NVMe |

PostgreSQL настраивается через `PG_*` в `.env` (уточнено после задач 2026-11-14): значения по
умолчанию — для 8 ГБ (`PG_MEM_LIMIT=2g`, `shared_buffers` 512 МБ); для 16 ГБ поднимите
`PG_MEM_LIMIT`, `PG_SHARED_BUFFERS`, `PG_EFFECTIVE_CACHE_SIZE` (см. `.env.example`).

Критичные узлы:

- **PostgreSQL** — INSERT в партиционированные логи (`audit_logs`, `user_activity`), нужен IOPS → NVMe.
- **FastStream-консьюмеры и боты** (`master_bot`, `bot_worker`).
- **Alloy** — сбор логов и метрик (`memory_limiter` 200 MiB).

Что занимает диск:

- файлы MinIO — объём каталога заранее неизвестен;
- `audit_logs` — хранение 24 месяца с `retention_keep_table = true`: отсоединённые партиции
  остаются на диске до архивации `make archive-audit`;
- `user_activity` — 3 месяца;
- файловые логи — 90 дней (`system_settings.file_logs_retention_days`);
- будущие бэкапы (Этап 8).

## Production VPS 2

urgentry, Tempo, Loki, Prometheus, Alertmanager, Grafana, Caddy, CrowdSec.

| Ресурс | Старт | Рост |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 2 ГБ + swap 2 ГБ | 4 ГБ |
| Диск | 30 ГБ SSD | 80+ ГБ SSD |

- В простое стек уже занимает ≈1.06 ГБ RAM.
- Loki (`retention_period: 720h`), Prometheus (`PROMETHEUS_RETENTION=30d`) и Tempo
  (`block_retention: 720h`) хранят данные 30 дней; объём растёт с трафиком.
  Alertmanager — `ALERTMANAGER_RETENTION` 720h.
- TLS в Caddy, CrowdSec и компакция Loki нагружают CPU — поэтому не 1 ядро.

## Лимиты памяти (уточнено после задач 2026-11-14)

Лимиты задаются `mem_limit` через переменные `*_MEM_LIMIT` (`.env.example`). Замер — после
пересоздания, в простое (`docker stats --no-stream`, dev-хост).

| Сервис | Лимит | Факт в простое |
|---|---|---|
| postgres | 2g (`PG_MEM_LIMIT`), `shm_size` 256m | 49 МБ |
| redis | 512m, `maxmemory` 256mb, `noeviction` | 6 МБ |
| rabbitmq | 768m, watermark 400 MiB absolute | 133 МБ |
| minio | 512m | 80 МБ |
| db_update / master_bot / bot_worker | 384m | 119 МБ |
| alloy | 512m (`memory_limiter` 200 MiB) | 150 МБ |
| docker-socket-proxy | 64m | 16 МБ |
| loki / prometheus | 512m / 512m | 78 / 85 МБ |
| grafana | 640m | 101 МБ |
| alertmanager / caddy | 128m / 128m | 23 / 19 МБ |
| urgentry / tempo / crowdsec | 256m / 512m / 256m (без изменений) | 29 / 54 / 219 МБ |

Сумма лимитов VPS 1 ≈ 5.0 ГБ (из 8 ГБ: остаток — page cache для PostgreSQL и ОС).

## Swap

Swap — страховка от OOM Killer при кратком пике, **не замена RAM**. На обоих серверах:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system
```

## Риски

| Риск | Статус | Факт |
|---|---|---|
| OOM без лимитов памяти | закрыто | `c9aa1af`, `dd048ae`: `mem_limit` у всех сервисов VPS 1 и VPS 2; после пересоздания все healthy, `make ready` 200, `make test-integration` 159 passed; максимум — crowdsec 85% лимита (лимит не менялся) |
| Redis `maxmemory` | закрыто | `c9aa1af`: `CONFIG GET` → `maxmemory 268435456`, `noeviction`. При заполнении запись отклоняется с OOM; переход на `volatile-lru` — отдельное решение |
| RabbitMQ watermark | закрыто | `c9aa1af`: `ops/rabbitmq/conf.d/20-memory.conf`; `rabbitmq-diagnostics status` → watermark 0.4194 gb |
| PostgreSQL на настройках по умолчанию | закрыто | `c9aa1af`: `SHOW` → shared_buffers 512MB, effective_cache_size 1536MB, work_mem 8MB, maintenance_work_mem 128MB, max_connections 100, wal_compression lz4, max_wal_size 2GB. Алерты на память и соединения PG не добавлены |
| Срок хранения urgentry | частично | `7ddd818`: GET settings → `eventRetentionDays` 30 (errors/logs/traces/profiles/outcomes 30). Нет алерта на размер `OBS_URGENTRY_DATA_DIR` (нет метрики размера каталога); SQLite не сжимается без VACUUM |
| Остатки GlitchTip (`obs/db`, `obs/redis`) | закрыто | `b136812`: каталоги удалены на dev-хосте; на других стендах — шаг в `docs/ops/observability.md` |
| Swap | частично | инструкция выше; на хостах не создан (нужен sudo на целевых VPS) |

## Как переизмерить

```bash
docker stats --no-stream   # RAM/CPU контейнеров
docker system df           # образы, контейнеры, тома, build cache
du -sh <каталог>           # каталоги из переменных *_DATA_DIR в .env
```
