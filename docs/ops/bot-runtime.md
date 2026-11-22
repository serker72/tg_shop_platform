# Среда выполнения ботов (bot runtime): пул прокси

## Контракт task-09: bounded dispatch

`app/entrypoints/bot_worker/commands.py` принимает dedupe claim с TTL 120 секунд и
ограничивает один вызов runtime adapter таймаутом 100 секунд (запас 20 секунд).
Превышение лимита отменяет adapter task, освобождает только claim своего owner и
возвращает transient `NackMessage(requeue=True)`. Поэтому обычная повторная
доставка не получает новый claim до завершения bounded dispatch. Продление claim
(heartbeat/renewal) намеренно не реализуется в task-09 и относится к task-10/11.


Боты магазинов (и, опционально, Master Bot) обращаются к Telegram через прокси (proxy), чтобы бан
одного IP не останавливал все боты. Код: `app/modules/bot_manager/proxy_service.py`
(`ProxyPoolService`), `proxy_health.py` (`ProxyHealthTracker`, `probe_handshake`),
`sessions.py` (`build_bot_session`), настройки `ProxySettings` (`app/core/config.py`).

## Пул и назначение
- Прокси хранятся в таблице `proxies` (URL зашифрован, уникальность по `url_hash`), управляются в
  панели Master Bot (право `proxies.manage`): добавить / забанить / активировать. Схемы: `socks5`,
  `socks4`, `http` (`scheme://[user:pass@]host:port`, порт обязателен; схема и хост
  приводятся к нижнему регистру до хеширования, поэтому `SOCKS5://Host:1` и `socks5://host:1` — один прокси);
  прокси `https://` не поддерживаются `aiohttp-socks`/`python-socks`.
- Бот привязывается через `bots.proxy_id`; нагрузка прокси — `COUNT` его живых ботов (не
  хранится). `assign_proxy` блокирует строку прокси (`FOR UPDATE`) и пересчитывает, поэтому лимит
  `PROXY_MAX_BOTS_PER_IP` соблюдается при параллельных назначениях. Привязка липкая (sticky): ACTIVE-прокси,
  не превысивший лимит, сохраняется. Порядок выбора: меньше страйков (strikes), затем меньше нагрузка, затем id.
- Нет свободного ACTIVE-прокси -> `NoProxyAvailableError` (бот не запускается).
- Логи, ошибки и `repr` показывают только `scheme://host:port` (`mask_proxy_url`).

## Сбои, страйки, сбой Telegram (outage)
- Поллер (poller) отмечает успех (`proxy:ok:{id}`) и, после `PROXY_FAILURE_THRESHOLD`
  ошибок подряд, сбой (`proxy:fail:{id}`) в Redis с TTL
  `PROXY_STRIKE_WINDOW_SECONDS`, затем вызывает `report_failure`.
- `report_failure` смотрит на ДРУГИЕ активные прокси:
  - ни один из них не был успешен в окне, или доля сбойных
    `>= PROXY_OUTAGE_RATIO` -> **outage** (недоступен Telegram/сеть, а не прокси): без
    страйка, gauge `telegram_outage=1` (сбрасывается первым успехом), лог уровня warning;
  - иначе -> **страйк** сообщившего прокси (не более одного за окно: Redis
    `proxy:strike:{id}` `SET NX EX`; без Redis — по `last_strike_at`).
  - Redis недоступен -> вердикт `error`, без страйка (fail-closed).
- **Пул из одного прокси не получает автоматических страйков**: вердикт `single_proxy`, только
  метрика/алерт; решение принимает оператор (забанить / заменить прокси).
- `strike_count >= PROXY_STRIKES_TO_QUARANTINE` -> `quarantine`, `last_ban_at=now`, все его
  боты отвязываются (их id возвращаются для перезапуска с другим прокси).
- `rehabilitate` (задача планировщика, task 03b) возвращает прокси, находящиеся в карантине дольше
  `PROXY_QUARANTINE_DAYS`, в ACTIVE с `strike_count=0` (идемпотентно). `banned` — только вручную.
- `probe_handshake` отличает мёртвый прокси (`proxy_dead`: не удаётся подключение/авторизация на прокси) от
  блокировки Telegram (`telegram_blocked`: туннель открыт, TLS/HTTP к `api.telegram.org` не проходит).

401 от Telegram — невалидный токен, никогда не страйк прокси.

## Метрики: какой процесс что экспортирует
| Серия | Процесс | Источник | Агрегация |
|---|---|---|---|
| `proxy_failure_verdicts_total{verdict}` (`strike`, `outage`, `single_proxy`, `deduplicated`, `error`) | `bot_worker` (каждая реплика) | счётчик в процессе | `sum by (verdict) (rate(...))` |
| `proxy_probe_total{result}` (`ok`, `proxy_dead`, `telegram_blocked`) | `bot_worker` | счётчик в процессе | `sum by (result) (rate(...))` |
| `telegram_outage` (0/1) | только `master_bot` | кеш в памяти результата `EXISTS proxy:outage`, опрос каждые 15 с | `max(telegram_outage)` |

Все значения меток экспортируются с `0`. Состояние outage общее: любой воркер устанавливает
`proxy:outage` (TTL `PROXY_STRIKE_WINDOW_SECONDS`) при вердикте `outage`, первый
успешный `mark_ok` любого воркера удаляет его. Ключи дедупликации страйков хранят токен владельца
(uuid4): неудачный страйк освобождает только свой ключ (Lua compare-and-delete). `master_bot` опрашивает ключ в фоновой задаче каждые 15 с (один асинхронный `EXISTS`, таймаут
2 с) и кеширует `(value, checked_at)`; `/metrics` только читает кеш и никогда не ждёт
Redis. Успешной проверки ещё не было или кеш старше 30 с (Redis недоступен) ->
`telegram_outage` не выводится (используйте `absent(telegram_outage)`), остальной `/metrics` работает.
Задача отменяется при остановке. Gauge может «мигать» (ключ outage установлен и снят
в пределах периода опроса или переустановлен следующим сбоем): `for:` правила
`TelegramUnreachable` (task 13) должен покрывать несколько периодов опроса.
Gauge пула из PostgreSQL (прокси по статусам, ёмкость) — `master_bot`, task 13.

## Порядок блокировок
Сначала строка прокси, затем строка бота (`assign_proxy`, `register_strike`, `ban`): нет взаимоблокировки (deadlock)
между параллельным назначением и карантином/баном. Ошибка БД внутри `report_failure` даёт
вердикт `error` (логируется, учитывается в метрике), а не исключение в поллер.

## Параметры (`.env`)
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MASTER_BOT_USE_PROXY` | `false` | Master Bot через `MASTER_BOT_PROXY_URL` |
| `MASTER_BOT_PROXY_URL` | пусто | секрет (`APP_SECRETS`), обязателен при включённом флаге |
| `TENANTS_USE_PROXY` | `false` | боты магазинов через пул `proxies` |
| `PROXY_MAX_BOTS_PER_IP` | `20` | ботов на один прокси |
| `PROXY_STRIKES_TO_QUARANTINE` | `2` | страйков до карантина |
| `PROXY_QUARANTINE_DAYS` | `7` | длительность карантина |
| `PROXY_STRIKE_WINDOW_SECONDS` | `3600` | окно дедупликации страйков и TTL отметок состояния |
| `PROXY_FAILURE_THRESHOLD` | `5` | ошибок бота подряд до `mark_fail` |
| `PROXY_OUTAGE_RATIO` | `0.5` | доля сбойных других прокси, означающая outage |

## Планировщик (scheduler; периодические задачи, только `bot_worker`)
Механизм: `app/core/scheduler` (`Schedule`, `ScheduledTask`, `TaskRegistry`,
`SchedulerRunner`); задачи и обработчики живут в модулях (`app/modules/bot_manager/tasks.py`).
Каждые `SCHEDULER_TICK_SECONDS` каждая реплика вычисляет плановый слот
`schedule.previous_fire(now)` (UTC) и выполняет `SET sched:lock:<task>:<slot ISO>Z <token> NX EX <ttl>`
(TTL = период + min(период, 1 ч); для ежедневных -> 25 ч). Победитель публикует
`{"task", "scheduled_at", "payload"}` в durable-очередь `tasks.<task>` (persistent-сообщение,
publisher confirms, `message_id=<task>:<slot>`); остальные учитывают `skipped`.

Добавление задачи:
1. В модуле: `registry.register(ScheduledTask(name, Schedule.daily_at("HH:MM") | Schedule.every(s), task_queue(name), payload_factory=..., catch_up=...))`.
2. Подписчик на ту же очередь: `broker.subscriber(RabbitQueue(task_queue(name), durable=True), ack_policy=AckPolicy.REJECT_ON_ERROR)`.
3. Подключить регистрацию в `app/entrypoints/bot_worker/scheduler.py`.

Гарантии — обработчики ОБЯЗАНЫ быть идемпотентными:
- не более одной успешной публикации на слот среди всех реплик; конкурирующие потребители (competing consumers) —
  сообщение обрабатывает одна реплика;
- ошибка публикации или отмена (остановка) -> блокировка снимается (compare-and-delete по токену),
  следующий тик повторяет попытку;
- таймаут подтверждения (confirm) -> сообщение может уже быть в очереди, повтор добавит дубликат;
- неоднозначный `SET` (записан, ответ потерян) -> слот остаётся заблокированным до истечения TTL: потерян (at-most-once);
- ошибка Redis (в т. ч. `OOM command not allowed` при `noeviction`) -> fail closed: без
  публикации, `log.error`, `result="error"`;
- `catch_up=True`: при старте публикуется уже прошедший слот (тот же ключ блокировки). Без него
  runner ждёт следующего слота. `every(s)` при `s < tick` пропускает слоты (warning при старте).
- Политика подтверждения `REJECT_ON_ERROR`: сбой обработчика логируется (`scheduled_task_failed` с
  `scheduled_at`), а сообщение отбрасывается — без цикла повторной доставки; следующий слот повторит работу.

Метрики (`bot_worker`): `scheduler_runs_total{task,result=published|skipped|error}` (все с
`0`; суммировать по репликам), `scheduler_last_success_timestamp_seconds{task}` — только на
реплике, выполнившей публикацию, используйте `max by (task) (...)`. Heartbeat `heartbeat:scheduler`
(некритичный, понижает readiness) каждые `OBSERVABILITY_HEARTBEAT_TTL_SECONDS / 3`.
Циклы планировщика контролируются `run_service`: упавший цикл останавливает процесс (exit 1), и
контейнер перезапускается. Остановка: циклы отменяются, затем брокер (`graceful_timeout` 15 с);
в compose `stop_grace_period: 30s`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | включение/выключение цикла тиков (подписчики потребляют в любом случае) |
| `SCHEDULER_TICK_SECONDS` | `30` | период тика |
| `PROXY_REHABILITATE_AT` | `03:00` | ежедневный `proxy.rehabilitate`, UTC (catch_up включён) |
