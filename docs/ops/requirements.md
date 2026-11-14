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

Лимиты задаются `mem_limit` через переменные `*_MEM_LIMIT` (`.env.example`). Факт — после
пересоздания, в простое (`docker stats --no-stream`, dev-хост, после правок ревью).

| Сервис | Лимит | Факт в простое |
|---|---|---|
| postgres | 2g (`PG_MEM_LIMIT`), `shm_size` 256m | 45 МБ |
| redis | 512m, `maxmemory` 256mb, `noeviction` | 6 МБ |
| rabbitmq | 768m, watermark 400 MiB absolute | 139 МБ |
| minio / minio_init (разовый) | 512m / 128m (`MINIO_INIT_MEM_LIMIT`) | 89 МБ / — |
| db_update (разовый) / master_bot / bot_worker | 384m (`APP_MEM_LIMIT`) | — / 118 / 118 МБ |
| alloy | 512m (`memory_limiter` 200 MiB) | 144 МБ |
| docker-socket-proxy | 64m (`DOCKER_SOCKET_PROXY_MEM_LIMIT`) | 17 МБ |
| docker-events | 32m (`DOCKER_EVENTS_MEM_LIMIT`) | 12 МБ |
| loki / prometheus | 512m / 512m | 84 / 73 МБ |
| grafana | 640m | 99 МБ |
| alertmanager / caddy | 128m / 128m | 24 / 15 МБ |
| urgentry / tempo | 256m / 512m (`URGENTRY_MEM_LIMIT` / `TEMPO_MEM_LIMIT`) | 29 / 58 МБ |
| crowdsec | 512m (`CROWDSEC_MEM_LIMIT`; было 256m, 219 МБ = 85%) | 52.09 МБ |

PostgreSQL укладывается в `PG_MEM_LIMIT` и в худшем случае: `max_connections` 50, `work_mem` 4MB,
`autovacuum_work_mem` 64MB — по формуле из `.env.example` ≈ 1.5 ГБ из 2 ГБ.

### Суммы лимитов

| Хост | Сумма `mem_limit` | RAM (старт / рекомендуемо) |
|---|---|---|
| VPS 1, постоянные сервисы | 5216 MiB ≈ 5.1 ГБ | 8 / 16 ГБ |
| VPS 1 во время `db_update` (+384m) и `minio_init` (+128m) | 5728 MiB ≈ 5.6 ГБ | 8 / 16 ГБ |
| VPS 2 | 3200 MiB ≈ 3.1 ГБ | 2 ГБ + swap 2 ГБ / 4 ГБ |
| Staging (VPS 1 + VPS 2 на одном хосте) | 8416 MiB ≈ 8.2 ГБ (с разовыми — 8.7 ГБ) | 4 ГБ + swap 2 ГБ / 8 ГБ |

**Лимиты — это потолки с overcommit, а не резерв.** Сумма может превышать RAM (VPS 2 и Staging
на старте): запас дают swap и то, что сервисы не достигают пика одновременно (в простое VPS 2 ≈
0.5 ГБ, Staging ≈ 1.3 ГБ). Лимит защищает хост от одного «убежавшего» контейнера: OOM Killer
срабатывает внутри его cgroup. Без `memswap_limit` Docker ставит `MemorySwap = 2 × mem_limit`,
то есть контейнер может дополнительно уйти в swap на размер своего `mem_limit`.

## Swap

Swap — страховка от OOM Killer при кратком пике, **не замена RAM**. На обоих серверах
(повторный запуск безопасен: строка в `/etc/fstab` добавляется один раз):

```bash
sudo fallocate -l 2G /swapfile   # XFS/btrfs: sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system
```

На XFS и btrfs файл от `fallocate` может не подойти для swap (`swapon: invalid argument`/«has
holes») — используйте `dd`. На btrfs файл должен быть ещё и без CoW (`chattr +C` до записи).
Контейнер без `memswap_limit` может использовать swap до размера своего `mem_limit` (см. выше).

## Риски (открытые задачи)

| Риск | Статус | Факт |
|---|---|---|
| OOM без лимитов памяти | закрыто | `c9aa1af`, `dd048ae`, `f57ddc0`: `mem_limit` у всех сервисов VPS 1 и VPS 2 (включая docker-socket-proxy, minio_init, urgentry, tempo, crowdsec через `*_MEM_LIMIT`); `make ready` 200 |
| CrowdSec у предела лимита | закрыто | `f57ddc0`: лимит 256m → 512m (`CROWDSEC_MEM_LIMIT`), после пересоздания 52.09 МБ (до — 219 МБ, 85%); bouncer работает (`make obs-bans`) |
| OOM-kill и перезапуски контейнеров не видны в мониторинге (нет метрик контейнеров) | закрыто | `9eff1ab`: `HostOomKill` по `node_vmstat_oom_kill`; `b8228d0`: sidecar `docker-events` (GET `/events` через docker-socket-proxy, без новых прав) → Loki; `72b91dd`: Loki ruler `ContainerOomKilled`/`ContainerExit137`/`ContainerRestartLoop`. Стенд 2026-11-14: `oom-probe` (`-m 16m`) → в Loki `action=oom` и `die exit_code=137`, `node_vmstat_oom_kill` 0→1 (повтор → 2), `HostOomKill` и Loki-алерты firing в Alertmanager; `ContainerRestartLoop` считает `die` с `cause!="docker_kill"` (перезапуск по политике даёт `die` → `start`, `restart` Docker пишет только на `docker restart`; проба `--restart=always` → firing). Классификатор причины `die` 137 (`cause` = `oom`/`docker_kill`/`unknown`, коммит «feat(obs): docker-events classifies die 137 cause»): нет ложного `ContainerExit137` на `docker stop`/`restart`; строка «OOM и перезапуски контейнеров» на `platform_overview`; см. docs/ops/observability.md |
| Redis `maxmemory` | закрыто | `c9aa1af`: `maxmemory 268435456`, `noeviction`. `df83c35`: при OOM Redis (`ResponseError`) и обрыве соединения запись heartbeat логируется warning `heartbeat_write_failed` и не роняет процесс; пропуск виден в `/health/ready` как `heartbeat:<component>`. Запись heartbeat (`publish_heartbeat`) пока нигде не вызывается — публикация воркерами и `WorkerHeartbeatMissing` в вехе II; обработка ошибок Redis готова заранее |
| RabbitMQ watermark | закрыто | `c9aa1af`: `ops/rabbitmq/conf.d/20-memory.conf`; watermark 0.4194 gb |
| PostgreSQL на настройках по умолчанию / выход за `mem_limit` | закрыто | `c9aa1af`, `3ab8b17`: `SHOW` → max_connections 50, work_mem 4MB, autovacuum_work_mem 64MB, shared_buffers 512MB; пулы приложения 5+5 на процесс. Тестовый стек — свой профиль 512m (`bfd462f`). Алерты на память и соединения PG не добавлены |
| Срок хранения urgentry | частично | `7ddd818`, `e89c8a4`: `eventRetentionDays` 30; настройка после записи `SENTRY_DSN`, ошибки — WARNING без остановки; `telemetryPolicies` сохраняются. Нет алерта на размер `OBS_URGENTRY_DATA_DIR`; SQLite не сжимается без VACUUM |
| Остатки GlitchTip (`obs/db`, `obs/redis`) | закрыто | `b136812`: удалены на dev-хосте; шаг в `docs/ops/observability.md` |
| Swap | частично | инструкция выше; на хостах не создан (нужен sudo на целевых VPS) |

## Как переизмерить

```bash
docker stats --no-stream   # RAM/CPU контейнеров
docker system df           # образы, контейнеры, тома, build cache
du -sh <каталог>           # каталоги из переменных *_DATA_DIR в .env
```
