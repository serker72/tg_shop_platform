# Сеть: VPS 1 ↔ VPS 2

- **VPS 1** — основной стек (`docker-compose.yml`): PostgreSQL, Redis, RabbitMQ, MinIO,
  `master_bot`, `bot_worker`, `db_update`.
- **VPS 2** — стек наблюдаемости (`docker-compose.obs.yml`): Loki, Prometheus,
  Alertmanager, Grafana, GlitchTip, Caddy. Подробности — [observability.md](observability.md).

Принцип: **наружу открыт только Caddy 80/443 на VPS 2** (и SSH), целиком за HTTP Basic Auth
(`CADDY_MODE=ip` — `https://<VPS_IP>/` и `/grafana/`, самоподписанный сертификат, `curl -k -u`;
`CADDY_MODE=domain` — домены с Let's Encrypt; см. [observability.md](observability.md)).
Sentry DSN — только внутренний адрес (Docker-сеть или VPN), публичный запрещён. Всё остальное слушает
`127.0.0.1` или VPN-адрес. Трафик VPS 1 → VPS 2 (логи, метрики, ошибки) и скрейп
VPS 2 → VPS 1 идут только через VPN.

## VPN

| Вариант | Плюсы | Минусы |
|---|---|---|
| **WireGuard** (рекомендуется) | в ядре Linux, нет внешних зависимостей и аккаунтов, статические адреса, минимум движущихся частей для 2 хостов | ключи и конфиги вручную, без ACL-сервера |
| Tailscale | установка за минуту, NAT traversal, ACL и MagicDNS | внешний координационный сервер (SaaS или self-hosted Headscale), адреса 100.x назначаются сервисом |

Для двух серверов с публичными IP рекомендуется WireGuard. Пример (`/etc/wireguard/wg0.conf`,
подсеть `10.10.0.0/24`, VPS 1 = `10.10.0.1`, VPS 2 = `10.10.0.2`; ключи генерируются
`wg genkey | tee priv | wg pubkey > pub` и в репозиторий не попадают):

```ini
# VPS 1
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = <приватный ключ VPS 1>

[Peer]
PublicKey = <публичный ключ VPS 2>
Endpoint = <публичный IP VPS 2>:51820
AllowedIPs = 10.10.0.2/32
PersistentKeepalive = 25
```

VPS 2 — зеркально (`Address = 10.10.0.2/24`, `AllowedIPs = 10.10.0.1/32`).
`sudo systemctl enable --now wg-quick@wg0`, проверка `wg show`, `ping 10.10.0.2`.

Tailscale: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`,
далее вместо `10.10.0.x` используются адреса `tailscale ip -4`, интерфейс `tailscale0`.

## Порты

Все публикации в compose — `127.0.0.1:${*_PORT_HOST}`; для доступа по VPN публикацию
переключают на VPN-IP хоста (`10.10.0.x:port`) через переменные/override-файл, а не на
`0.0.0.0`.

### VPS 1 (`docker-compose.yml`)

| Сервис | Порт (контейнер) | Хост по умолчанию | Доступ |
|---|---|---|---|
| PostgreSQL | 5432 | `127.0.0.1:${POSTGRES_PORT_HOST:-5432}` | только локально |
| Redis | 6379 | `127.0.0.1:${REDIS_PORT_HOST:-6379}` | только локально |
| RabbitMQ AMQP | 5672 | `127.0.0.1:${RABBITMQ_PORT_HOST:-5672}` | только локально |
| RabbitMQ management | 15672 | `127.0.0.1:${RABBITMQ_MGMT_PORT_HOST:-15672}` | SSH-туннель |
| MinIO API | 9000 | `127.0.0.1:${MINIO_API_PORT_HOST:-9000}` | только локально |
| MinIO console | 9001 | `127.0.0.1:${MINIO_CONSOLE_PORT_HOST:-9001}` | SSH-туннель |
| `/health/*`, `/metrics` приложения | `${OBSERVABILITY_HTTP_PORT:-8080}` | — / VPN-IP | Prometheus VPS 2 через VPN |
| WireGuard | 51820/udp | `0.0.0.0` | только с IP VPS 2 |

### VPS 2 (`docker-compose.obs.yml`)

| Сервис | Хост по умолчанию | Доступ |
|---|---|---|
| Grafana | `127.0.0.1:${GRAFANA_PORT_HOST:-3000}` | через Caddy (HTTPS) |
| GlitchTip web | `${OBS_INGEST_BIND_ADDRESS:-127.0.0.1}:${GLITCHTIP_PORT_HOST:-8000}` | через Caddy (HTTPS); приём событий Sentry SDK с VPS 1 — через VPN |
| Loki | `127.0.0.1:${LOKI_PORT_HOST:-3100}` | VPS 1 (Alloy/push логов) — только через VPN |
| Prometheus | `127.0.0.1:${PROMETHEUS_PORT_HOST:-9090}` | remote write / UI — только VPN или SSH-туннель |
| Alertmanager | `127.0.0.1:${ALERTMANAGER_PORT_HOST:-9093}` | только локально (исходящие в Telegram) |
| obs-postgres, obs-redis | не публикуются | внутренняя сеть compose |
| **Caddy** | `${CADDY_BIND_ADDRESS:-0.0.0.0}:80`, `:443` | **публично**, Basic Auth на всё |
| WireGuard | `0.0.0.0:51820/udp` | только с IP VPS 1 |

Не публикуются в интернет ни при каких условиях: `/metrics` и `/health/*` приложения,
Alloy, Loki push API, Prometheus (включая remote write endpoint), Alertmanager,
PostgreSQL/Redis/RabbitMQ/MinIO. Caddy проксирует только Grafana и GlitchTip (за Basic Auth,
без исключений для API; API GlitchTip / `sentry-cli` — через SSH на `127.0.0.1:8000`).

Важно: Docker публикует порты через собственные цепочки iptables (`DOCKER`), в обход
правил ufw. Поэтому защита строится в первую очередь на адресе публикации
(`127.0.0.1`/VPN-IP), а firewall — второй рубеж; для публикаций на `0.0.0.0` ufw не
поможет без правил в цепочке `DOCKER-USER`.

## Firewall

### ufw (VPS 2)

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp                                    # лучше: from <admin IP>
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow from <публичный IP VPS 1> to any port 51820 proto udp
sudo ufw allow in on wg0 from 10.10.0.1 to 10.10.0.2 port 3100 proto tcp   # Loki
sudo ufw allow in on wg0 from 10.10.0.1 to 10.10.0.2 port 9090 proto tcp   # Prometheus remote write
sudo ufw allow in on wg0 from 10.10.0.1 to 10.10.0.2 port 8000 proto tcp   # GlitchTip (Sentry SDK)
sudo ufw enable
```

### ufw (VPS 1)

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow from <публичный IP VPS 2> to any port 51820 proto udp
sudo ufw allow in on wg0 from 10.10.0.2 to 10.10.0.1 port 8080 proto tcp   # скрейп /metrics
sudo ufw enable
```

Боты Telegram работают через polling/исходящие соединения — входящие порты на VPS 1 не
нужны. Если появится webhook — он принимается только через reverse proxy с TLS.

### nftables (эквивалент, VPS 2)

```nft
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    tcp dport 22 accept
    tcp dport { 80, 443 } accept
    udp dport 51820 ip saddr <публичный IP VPS 1> accept
    iifname "wg0" ip saddr 10.10.0.1 tcp dport { 3100, 9090, 8000 } accept
    icmp type echo-request accept
  }
}
```

Для контейнерных портов, опубликованных на `0.0.0.0` (Caddy), фильтрация — в цепочке
`DOCKER-USER` (iptables) или правилами `forward` в nftables.

## Синхронизация времени

**Обязательна на обоих хостах.** Heartbeat воркеров записывается одной машиной и
проверяется другой (`/health/ready`, алерты Prometheus): если часы расходятся больше чем на
`OBSERVABILITY_HEARTBEAT_TTL_SECONDS` (по умолчанию 30 с), здоровый воркер считается
мёртвым — ложный fail readiness и ложные алерты. Также рассинхрон ломает порядок логов в
Loki и интервалы метрик.

```bash
sudo apt-get install -y chrony
sudo systemctl enable --now chrony
chronyc tracking            # "System time" — смещение, должно быть << 1 с
chronyc sources -v
timedatectl                 # "System clock synchronized: yes"
```

Используется один и тот же пул NTP на обоих хостах (например, `pool.ntp.org` или пул
провайдера); `systemd-timesyncd` при этом отключается (`timedatectl set-ntp false` перед
установкой chrony не требуется — пакет chrony заменяет его). Часовой пояс серверов — UTC.
Контроль: `chronyc tracking` в чек-листе деплоя; при появлении node exporter — алерт на
`node_timex_offset_seconds` > 1 с (node exporter в текущем obs-стеке не развёрнут).
