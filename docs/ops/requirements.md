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

RAM 16 ГБ имеет смысл только после настройки PostgreSQL под объём RAM: сейчас
`shared_buffers` = 128 МБ (значение по умолчанию), и лишняя память базе не достанется.

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

## Риски (открытые задачи)

Все пункты запланированы к исправлению.

- **Нет лимитов памяти на VPS 1.** В compose `mem_limit` задан только у urgentry (256m),
  tempo (512m) и crowdsec (256m) — все на VPS 2. У Redis не задан `maxmemory` (политика
  `noeviction`), у RabbitMQ `vm_memory_high_watermark` по умолчанию (0.6 RAM). Итог — риск,
  что OOM Killer завершит PostgreSQL.
- **PostgreSQL на настройках по умолчанию** (`shared_buffers` 128 МБ, `work_mem` 4 МБ,
  `max_connections` 100).
- **У urgentry не настроен срок хранения** — его SQLite растёт без ограничения.
- **Swap** рекомендован на обоих серверах как страховка от OOM, но не как замена RAM.

## Как переизмерить

```bash
docker stats --no-stream   # RAM/CPU контейнеров
docker system df           # образы, контейнеры, тома, build cache
du -sh <каталог>           # каталоги из переменных *_DATA_DIR в .env
```
