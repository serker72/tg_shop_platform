# Bot runtime: proxy pool

Shop bots (and optionally the Master Bot) reach Telegram through proxies so that a ban of
one IP does not stop every bot. Code: `app/modules/bot_manager/proxy_service.py`
(`ProxyPoolService`), `proxy_health.py` (`ProxyHealthTracker`, `probe_handshake`),
`sessions.py` (`build_bot_session`), settings `ProxySettings` (`app/core/config.py`).

## Pool and assignment
- Proxies live in the `proxies` table (URL encrypted, unique by `url_hash`), managed in the
  Master Bot panel (permission `proxies.manage`): add / ban / activate. Schemes: `socks5`,
  `socks4`, `http` (`scheme://[user:pass@]host:port`, port required; scheme and host are
  lower-cased before hashing, so `SOCKS5://Host:1` and `socks5://host:1` are one proxy);
  `https://` proxies are not supported by `aiohttp-socks`/`python-socks`.
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

401 from Telegram is an invalid token, never a proxy strike.

## Metrics: which process exports what
| Series | Process | Source | Aggregation |
|---|---|---|---|
| `proxy_failure_verdicts_total{verdict}` (`strike`, `outage`, `single_proxy`, `deduplicated`, `error`) | `bot_worker` (every replica) | in-process counter | `sum by (verdict) (rate(...))` |
| `proxy_probe_total{result}` (`ok`, `proxy_dead`, `telegram_blocked`) | `bot_worker` | in-process counter | `sum by (result) (rate(...))` |
| `telegram_outage` (0/1) | `master_bot` only | in-memory cache of `EXISTS proxy:outage`, polled every 15 s | `max(telegram_outage)` |

All label values are exported with `0`. The outage state is shared: any worker sets
`proxy:outage` (TTL `PROXY_STRIKE_WINDOW_SECONDS`) on an `outage` verdict, the first
successful `mark_ok` of any worker deletes it. Strike dedup keys hold an owner token
(uuid4): a failed strike frees only its own key (Lua compare-and-delete). `master_bot` polls the key in a background task every 15 s (one async `EXISTS`, timeout
2 s) and caches `(value, checked_at)`; `/metrics` only reads the cache and never waits for
Redis. No successful check yet or the cache older than 30 s (Redis unreachable) ->
`telegram_outage` is omitted (use `absent(telegram_outage)`), the rest of `/metrics` works.
The task is cancelled on shutdown. The gauge may blink (an outage key set and cleared
within a poll period, or re-set by the next failure): the `for:` of the
`TelegramUnreachable` rule (task 13) must cover several poll periods.
Pool gauges from PostgreSQL (proxies by status, capacity) - `master_bot`, task 13.

## Lock order
Proxy row first, then the bot row (`assign_proxy`, `register_strike`, `ban`): no deadlock
between parallel assignment and quarantine/ban. A DB error inside `report_failure` gives
the verdict `error` (logged, counted), never an exception to the poller.

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

## Scheduler (periodic tasks, `bot_worker` only)
Mechanism: `app/core/scheduler` (`Schedule`, `ScheduledTask`, `TaskRegistry`,
`SchedulerRunner`); tasks and handlers live in the modules (`app/modules/bot_manager/tasks.py`).
Every `SCHEDULER_TICK_SECONDS` each replica computes the planned slot
`schedule.previous_fire(now)` (UTC) and runs `SET sched:lock:<task>:<slot ISO>Z <token> NX EX <ttl>`
(TTL = period + min(period, 1 h); daily -> 25 h). The winner publishes
`{"task", "scheduled_at", "payload"}` to the durable queue `tasks.<task>` (persistent message,
publisher confirms, `message_id=<task>:<slot>`); the others count `skipped`.

Adding a task:
1. In the module: `registry.register(ScheduledTask(name, Schedule.daily_at("HH:MM") | Schedule.every(s), task_queue(name), payload_factory=..., catch_up=...))`.
2. Subscriber on the same queue: `broker.subscriber(RabbitQueue(task_queue(name), durable=True), ack_policy=AckPolicy.REJECT_ON_ERROR)`.
3. Wire the registration into `app/entrypoints/bot_worker/scheduler.py`.

Guarantees - handlers MUST be idempotent:
- at most one successful publication per slot across replicas; competing consumers - one
  replica handles a message;
- publish error or cancel (shutdown) -> the lock is released (token compare-and-delete), the
  next tick retries;
- confirm timeout -> the message may already be queued, the retry adds a duplicate;
- ambiguous `SET` (written, reply lost) -> the slot stays locked until TTL: lost (at-most-once);
- Redis error (incl. `OOM command not allowed` under `noeviction`) -> fail closed: no
  publication, `log.error`, `result="error"`;
- `catch_up=True`: on start the already passed slot is published (same lock key). Without it
  the runner waits for the next slot. `every(s)` with `s < tick` skips slots (warning at start).
- Ack policy `REJECT_ON_ERROR`: a failing handler is logged (`scheduled_task_failed` with
  `scheduled_at`) and the message dropped - no redelivery loop; the next slot repeats it.

Metrics (`bot_worker`): `scheduler_runs_total{task,result=published|skipped|error}` (all with
`0`; sum across replicas), `scheduler_last_success_timestamp_seconds{task}` - only on the
replica that published, use `max by (task) (...)`. Heartbeat `heartbeat:scheduler`
(non-critical, degrades readiness) every `OBSERVABILITY_HEARTBEAT_TTL_SECONDS / 3`.
Scheduler loops are supervised by `run_service`: a dead loop stops the process (exit 1) and
the container is restarted. Stop: loops cancelled, then the broker (`graceful_timeout` 15 s);
compose `stop_grace_period: 30s`.

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | tick loop on/off (subscribers consume anyway) |
| `SCHEDULER_TICK_SECONDS` | `30` | tick period |
| `PROXY_REHABILITATE_AT` | `03:00` | daily `proxy.rehabilitate`, UTC (catch_up on) |
