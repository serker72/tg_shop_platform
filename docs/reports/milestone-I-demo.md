# Веха I — сценарий демонстрации заказчику (15–20 минут)

## Подготовка (за день до демо)

1. Чистый хост (или чистый каталог данных); установлены Docker, `make`, `openssl`, `python3`, git.
   Шаги с `pytest`/`uv` — подготовка п. 5 (`make test-report`), демонстрация шаги 2
   (`make test-integration`) и 6 (`uv run pytest ...`): нужны [uv](https://docs.astral.sh/uv/)
   и dev-зависимости — `make install`.
2. `make env` — сгенерировать `.env` (секреты случайные), заполнить `MASTER_BOT_TOKEN`.
   **Пароль Basic Auth мониторинга `make env` выводит один раз — сразу сохранить** в менеджер
   паролей. `make obs-auth` — только если пароль потерян (задаёт новый, пересоздаёт Caddy).
3. В `.env`:
   - `PROJECT_DATA_DIR` — каталог данных; по умолчанию `/opt/tg_shop_platform_data`, нужен
     `sudo mkdir` + `chown` на пользователя (README, п. 2) или другой каталог с правами записи;
   - `PROJECT_ENVIRONMENT=stage` (или `loc`); при `test` bootstrap urgentry **не пишет**
     `SENTRY_DSN` и шаг 4 не сработает;
   - `CADDY_MODE=ip` и `VPS_IP=<IP хоста>` — `make env` оставляет `VPS_IP` пустым, заполнить
     (или режим доменов — см. README).
4. Режим «всё на одном хосте» (отдельного сервера мониторинга пока нет) — в `.env`:
   - `ALLOY_SINGLE_HOST=1`;
   - **заменить значения** существующих строк (не добавлять вторые — при дублях действует
     последняя): `OBS_LOKI_PUSH_URL=http://loki:3100/loki/api/v1/push`,
     `OBS_PROMETHEUS_REMOTE_WRITE_URL=http://prometheus:9090/api/v1/write`.
5. Строго в этом порядке:
   - `make obs-up` — стек мониторинга (Grafana, Loki, Prometheus, Alertmanager, Tempo, urgentry,
     Caddy); в конце bootstrap urgentry **записывает `SENTRY_DSN` в `.env`**;
   - `make agent-up` — основной стек + агент Alloy (уже с новым `SENTRY_DSN`), в конце
     `make ready`. Обратный порядок оставит приложение без DSN и агента без сети мониторинга.
6. `make test-report` — свежий отчёт об автотестах (`build/reports/milestone-I-tests.md`;
   принятая версия — [milestone-I-tests.md](milestone-I-tests.md)); убедиться, что все ✅.
7. Заранее открыть вкладки: Grafana, urgentry, отчёт; проверить вход по Basic Auth.

## Ход демонстрации

### 1. Развёртывание одной командой (3 мин)
- Показать: `make help`; стек уже поднят вчерашним `make agent-up` (повторный запуск той же
  командой идемпотентен). **Не запускать `make up`** — он поднимет стек без агента наблюдаемости.
- `make ready` — ждёт, пока `master_bot` и `bot_worker` ответят «готов».
- `docker compose ps -a` — все сервисы `healthy`, `db_update` в состоянии `Exited (0)`
  (миграции применены).
- Говорить: «Вся серверная часть — PostgreSQL 17, Redis, RabbitMQ, MinIO и приложение —
  поднимается одной командой; миграции базы применяются автоматически».

### 2. Автотесты вживую (4 мин)
- `make test-integration` — поднимает изолированный тестовый стек (отдельные данные и порты) и
  прогоняет интеграционные тесты против настоящих PostgreSQL/Redis/RabbitMQ/MinIO.
- Открыть отчёт: итоги и матрица «пункт этапа → тесты → результат». Раскрыть `<details>` у R2
  (разделение магазинов) и S4 (шифрование).
- После демо тестовый стек: `make test-integration-down`.

### 3. Дашборды Grafana (3 мин)
- Открыть `https://<VPS_IP>/grafana/` (или домен Grafana): браузер запрашивает логин/пароль
  (Basic Auth), затем вход в Grafana.
- Dashboards → папка **tg_shop_platform** (папка provisioning, не дашборд) → три дашборда:
  обзор платформы (uid `tgsp-overview`: состояние сервисов, ошибки),
  **PostgreSQL / Redis / RabbitMQ** (uid `tgsp-datastores`, партиции pg_partman), журналы
  (uid `tgsp-logs`: логи приложения из Loki, фильтр по сервису).

### 4. Трассировка и журнал ошибок urgentry (3 мин)
- `make otel-smoke` — в контейнере `master_bot` создаётся span, выполняется SQL, сообщение
  публикуется и принимается через RabbitMQ, отправляется тестовая ошибка в urgentry; в конце
  печатаются `TRACE_ID <32 hex>` и `SENTRY_EVENT_ID` (секреты не выводятся).
- Grafana → дашборд **Tracing RED** (uid `tracing-red`): запросы/ошибки/латентность
  `master_bot` по span-метрикам Tempo (обновление ~1 мин).
- Explore → источник *Tempo* → вставить `TRACE_ID`: дерево `otel.smoke` → `connect`/`select` →
  `otel-smoke-q publish` → `process`. Кнопка «Logs for this span» → логи Loki с тем же `trace_id`
  (или Explore → Loki: `{service=~".+"} | trace_id="<TRACE_ID>"`).
- urgentry (`https://<VPS_IP>/`) → issue `RuntimeError: otel smoke forced error`; в событии
  контекст `trace` с тем же `trace_id`; секреты замаскированы.

### 5. Оповещения о сбоях (2 мин)
- `make test-alerts` — promtool проверяет правила алертов и их unit-тесты (все SUCCESS).
- Показать правило `PgPartmanBgwDown` в `ops/observability/prometheus/rules/platform.yml`.
- Живые правила — в Grafana (Prometheus и Alertmanager наружу не опубликованы, только через
  Caddy → Grafana): **Alerting → Alert rules** → раздел правил источника данных *Prometheus*
  (data source-managed, 13 правил; раздел Grafana-managed пуст — это нормально).
  **Explore** → источник *Prometheus* → запрос `ALERTS` (активные/ожидающие алерты) или
  `pg_partman_bgw_up` / `up`. Резервный вариант для инженера:
  `ssh -L 9090:127.0.0.1:9090 <хост>` и `http://127.0.0.1:9090/alerts`.
- Говорить: «если фоновый процесс, создающий разделы журналов, остановился — придёт
  оповещение». Доставка в Telegram — после появления отдельного сервера.

### 6. Безопасность (3 мин)
- **Токены зашифрованы:** таблицы ботов появятся на Этапе II, поэтому показываем механизм тестом:
  `uv run pytest tests/unit/test_encrypted_string.py -v` — после смены ключа старые значения
  читаются, новые пишутся новым ключом. «В БД не открытый текст» показывает
  `test_round_trip_encrypts_and_decrypts`: он читает сырое значение из таблицы в обход ORM и
  проверяет, что открытого текста там нет. Колонка `Bot.encrypted_token` появится на Этапе II —
  тогда то же будет видно `psql`-запросом (строка Fernet `gAAAA…`).
- **Изоляция магазинов:** `make test-integration` уже прогнал
  `tests/integration/test_tenant_isolation_repository.py`; показать строку R2 в отчёте.
- **Права:** строки R3/S5 — роли, персональные права, кэш; при недоступном Redis права
  читаются из PostgreSQL.

### 7. Что вне вехи I и когда (1 мин)
- Отдельный сервер мониторинга через VPN и алерты в Telegram — когда сервер будет выделен.
- Экран «фактические права», журнал действий с фильтрами и выгрузкой TXT, CI/CD — Этап II.
- Сбой логирования при покупке, финансовый журнал — Этап III.
- Резервное копирование по расписанию — Этап IV.

## Частые вопросы заказчика

- **Что если упадёт Redis?** Проверка прав продолжит работать через PostgreSQL (тест S5).
- **Не потеряются ли журналы?** Запись буферизуется; если запись не удалась, работа не
  прерывается, потеря учитывается в метрике и вызывает оповещение (`LogsDroppedIncreasing`,
  `LogsDroppedHigh`).
- **Где хранятся ключи?** В `.env` на сервере, не в базе и не в git; ключ можно сменить.
- **Видит ли один магазин данные другого?** Нет — это проверяется автотестами (R2).
- **Есть ли резервные копии?** В вехе I проверена воспроизводимость восстановления; копирование
  по расписанию — Этап IV.
- **Кто видит мониторинг?** Только по HTTPS и логину/паролю; ошибки приложения передаются по
  внутренней сети.
