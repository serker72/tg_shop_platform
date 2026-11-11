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

## Образы

Закреплены tag@digest, переопределяются `*_IMAGE` (список — в `.env.example`).
