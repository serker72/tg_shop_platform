# Bot runtime: proxy pool

Shop bots (and optionally the Master Bot) reach Telegram through proxies so that a ban of
one IP does not stop every bot. Code: `app/modules/bot_manager/proxy_service.py`
(`ProxyPoolService`), `proxy_health.py` (`ProxyHealthTracker`, `probe_handshake`),
`sessions.py` (`build_bot_session`), settings `ProxySettings` (`app/core/config.py`).

## Pool and assignment
- Proxies live in the `proxies` table (URL encrypted, unique by `url_hash`), managed in the
  Master Bot panel (permission `proxies.manage`): add / ban / activate. Schemes: `socks5`,
  `socks4`, `http` (`scheme://[user:pass@]host:port`); `https://` proxies are not supported
  by `aiohttp-socks`/`python-socks`.
- A bot is bound by `bots.proxy_id`; the load of a proxy is `COUNT` of its live bots (not
  stored). `assign_proxy` locks the proxy row (`FOR UPDATE`) and recounts, so the limit
  `PROXY_MAX_BOTS_PER_IP` holds under parallel assignments. Binding is sticky: an ACTIVE,
  not overfull proxy is kept. Choice order: fewer strikes, then lower load, then id.
- No free ACTIVE proxy -> `NoProxyAvailableError` (the bot is not started).
- Logs, errors and `repr` show only `scheme://host:port` (`mask_proxy_url`).

## Failures, strikes, outage
- The poller marks success (`proxy:ok:{id}`) and, after `PROXY_FAILURE_THRESHOLD`
  consecutive errors, failure (`proxy:fail:{id}`) in Redis with TTL
  `PROXY_STRIKE_WINDOW_SECONDS`, then calls `report_failure`.
- `report_failure` looks at the OTHER active proxies:
  - none of them succeeded in the window, or the share of failed ones is
    `>= PROXY_OUTAGE_RATIO` -> **outage** (Telegram/network is down, not the proxy): no
    strike, gauge `telegram_outage=1` (cleared by the first success), warning log;
  - otherwise -> **strike** of the reporting proxy (at most one per window: Redis
    `proxy:strike:{id}` `SET NX EX`; without Redis - by `last_strike_at`).
  - Redis unavailable -> verdict `error`, no strike (fail-closed).
- **A pool of one proxy gets no automatic strikes**: verdict `single_proxy`, only the
  metric/alert; an operator decides (ban / replace the proxy).
- `strike_count >= PROXY_STRIKES_TO_QUARANTINE` -> `quarantine`, `last_ban_at=now`, all its
  bots are unbound (their ids are returned for restart with another proxy).
- `rehabilitate` (scheduler task, task 03b) returns proxies quarantined longer than
  `PROXY_QUARANTINE_DAYS` to ACTIVE with `strike_count=0` (idempotent). `banned` - manual only.
- `probe_handshake` tells a dead proxy (`proxy_dead`: connect/auth to the proxy fails) from a
  Telegram block (`telegram_blocked`: tunnel opened, TLS/HTTP to `api.telegram.org` fails).

Metrics: `proxy_failure_verdicts_total{verdict}`, `proxy_probe_total{result}`,
`telegram_outage`. 401 from Telegram is an invalid token, never a proxy strike.

## Parameters (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `MASTER_BOT_USE_PROXY` | `false` | Master Bot through `MASTER_BOT_PROXY_URL` |
| `MASTER_BOT_PROXY_URL` | empty | secret (`APP_SECRETS`), required when the flag is on |
| `TENANTS_USE_PROXY` | `false` | shop bots through the `proxies` pool |
| `PROXY_MAX_BOTS_PER_IP` | `20` | bots per proxy |
| `PROXY_STRIKES_TO_QUARANTINE` | `2` | strikes before quarantine |
| `PROXY_QUARANTINE_DAYS` | `7` | quarantine length |
| `PROXY_STRIKE_WINDOW_SECONDS` | `3600` | dedup window of strikes and TTL of health marks |
| `PROXY_FAILURE_THRESHOLD` | `5` | consecutive errors of a bot before `mark_fail` |
| `PROXY_OUTAGE_RATIO` | `0.5` | share of failed other proxies meaning an outage |
