# System Prompt & Architecture Specification (Master Document)

**Контекст проекта (Project Overview)**
Разработка высоконагруженной мульти-арендной (Multi-tenant) SaaS-платформы для Telegram-ботов. Управление строго через Master Bot (без Web UI) с использованием `aiogram-dialog`. Динамический запуск клиентских ботов в режиме polling.
Особенность платформы — полноценный динамический конструктор ролей (Dynamic RBAC) и использование мягкого удаления (`soft delete`) для всех сущностей.

**Технологический стек и Паттерны**

* **Стек:** Python 3.13, uv, PostgreSQL 17, Redis, RabbitMQ, aiogram 3, aiogram-dialog, SQLAlchemy 2.0, Alembic, Dishka, FastStream.
* **Паттерны:** DDD, Clean Architecture, Bounded Contexts, Saga & Transactional Outbox, Dynamic RBAC.

---

## Структура проекта

Структура проекта строится на принципах **Clean Architecture** и **Domain-Driven Design (DDD)**. Кодовая база разделена на независимые Bounded Contexts (модули), которые общаются друг с другом через интерфейсы или шину событий (FastStream/RabbitMQ), а зависимости внедряются через IoC-контейнер (Dishka).

```text
tg_shop_platform/
├── pyproject.toml                 # Конфигурация зависимостей (uv)
├── docker-compose.yml             # Инфраструктура (PostgreSQL, Redis, RabbitMQ, Воркеры)
├── alembic.ini                    # Конфигурация миграций
├── migrations/                    # КАТАЛОГ МИГРАЦИЙ (Alembic)
│   ├── env.py                     # Скрипт настройки окружения миграций
│   ├── script.py.mako             # Шаблон для генерации новых миграций
│   └── versions/                  # Сами файлы миграций (например, 123_add_users.py)
├── ops/                           # Инфраструктура и деплой
|   ├── observability/             # Конфигурация для второго VPS
|   |   ├── docker-compose.obs.yml
|   |   ├── prometheus/            # Конфиги Prometheus
|   |   ├── grafana/               # Дашборды
|   |   ├── tempo/                 # Tempo (трейсы)
|   |   └── urgentry/              # Dockerfile urgentry (заменил GlitchTip)
|   └── scripts/                   # Bash-скрипты (deploy.sh, backup.sh, wait-for-it.sh)
└── app/
    ├── core/                      # Глобальные настройки
    │   ├── config.py              # pydantic-settings (.env)
    │   └── logging.py             # Настройка структурированного логгирования
    │
    ├── shared_kernel/             # Разделяемое ядро (базовые абстракции для всех модулей)
    │   ├── domain/
    │   │   ├── base_entity.py     # BaseEntity (created_at, updated_at, is_active, is_deleted)
    │   │   └── exceptions.py      # Базовые доменные ошибки
    │   └── infrastructure/
    │       ├── database.py        # Настройка SQLAlchemy AsyncEngine
    │       └── base_repository.py # BaseRepository (реализация soft_delete и базовых CRUD)
    │
    ├── entrypoints/               # Точки входа в приложение (Deployment Units)
    │   ├── master_bot/            # Контейнер 1: Системный бот (aiogram-dialog)
    │   │   └── main.py
    │   ├── bot_worker/            # Контейнер 2: Polling-воркер клиентских ботов
    │   │   └── main.py
    │   └── cli/                   # Скрипты инициализации (setup.py)
    │
    └── modules/                   # Модульная архитектура (ТЗ, п. 10)
        ├── iam/                   # Управление доступом, тенанты, роли, RBAC-полномочия
        ├── bot_manager/           # Жизненный цикл ботов (start/stop), поллинг, защита от банов
        ├── catalog/               # Каталог, категории, товары (products)
        ├── orders/                # Корзина, заказы, позиции заказов (Saga/Outbox)
        ├── billing/               # Кошельки, транзакции, интеграция с платежками (Леджер)
        ├── marketing/             # Скидки, промокоды, лотереи, реферальные программы
        └── support/               # Тикеты (cases), сообщения, отзывы, FAQ

```

### Детальная структура каталога модуля (на примере `catalog` / `products`)

Каждый модуль внутри `app/modules/` является самодостаточным микро-приложением. Он строго соблюдает правило зависимости (Dependency Rule): внешние слои (`presentation`, `infrastructure`) знают о внутренних (`application`, `domain`), но внутренние ничего не знают о внешних.

```text
app/modules/catalog/
├── __init__.py
├── ioc.py                         # Dishka Provider: сборка зависимостей именно для каталога
│
├── domain/                        # ДОМЕННЫЙ СЛОЙ (Центр архитектуры, без внешних библиотек)
│   ├── entities.py                # Чистые python-dataclasses: Product, Category
│   ├── exceptions.py              # ProductNotFoundError, OutOfStockError
│   └── interfaces.py              # Абстрактные классы: IProductRepository, ICategoryRepository
│
├── application/                   # ПРИКЛАДНОЙ СЛОЙ (Юзкейсы и бизнес-логика)
│   ├── dto.py                     # Объекты передачи данных (ProductCreateDTO, CategoryUpdateDTO)
│   ├── use_cases/                 # Сценарии использования
│   │   ├── create_product.py      # Логика создания товара (валидация, вызов репозитория)
│   │   ├── get_tenant_catalog.py  # Получение витрины для конкретного магазина
│   │   └── update_stock.py        # Изменение остатков при покупке
│   └── events.py                  # Доменные события (например, ProductCreatedEvent)
│
├── infrastructure/                # ИНФРАСТРУКТУРНЫЙ СЛОЙ (Реализация интерфейсов, БД)
│   ├── models.py                  # SQLAlchemy модели (CategoryModel, ProductModel с foreign keys)
│   └── repositories.py            # ProductRepository (имплементирует IProductRepository через AsyncSession)
│
└── presentation/                  # СЛОЙ ПРЕДСТАВЛЕНИЯ (Telegram, RabbitMQ)
    ├── tg_master/                 # Интерфейс для администраторов (в Master Bot)
    │   ├── dialogs.py             # Стейт-машины aiogram-dialog (добавление товара, загрузка фото)
    │   └── getters.py             # Функции получения данных для отрисовки кнопок (с учетом RBAC)
    │
    ├── tg_client/                 # Интерфейс для покупателей (в запущенных ботах-витринах)
    │   ├── routers.py             # Обычные aiogram-роутеры (Inline-клавиатуры для скорости)
    │   └── keyboards.py           # Генераторы пагинации и кнопок витрины
    │
    └── message_broker/            # Обработчики событий из других модулей (FastStream)
        └── consumers.py           # Слушатель события OrderPaidEvent (чтобы списать stock_quantity)

```

**Особенности взаимодействия:**

* Если модулю `orders` нужно проверить наличие товара перед созданием заказа, он не лезет напрямую в `models.py` модуля `catalog`. Он обращается к интерфейсу `IProductRepository` или вызывает соответствующий `UseCase` каталога через внедренный зависимостью Dishka-контейнер.
* Паттерн **Outbox / Saga** реализуется на стыке `infrastructure` (запись в `outbox_messages`) и `presentation/message_broker` (публикация и чтение сообщений FastStream).

---

Вот готовый текст пункта для плана разработки (System Prompt), который вы можете передать AI-ассистенту. Он описывает настройку инфраструктуры, работу с переменными окружения и создание `Makefile`.

---

### Автоматизация инфраструктуры, Makefile и Тестовое окружение

**Цель:** Реализовать единый инфраструктурный пайплайн для локальной разработки и тестирования с использованием `docker-compose`, жесткой валидацией путей (fail-fast) и автоматизацией через `Makefile`.

**Задачи для AI-ассистента:**

1. **Создание шаблона `.env` (Persistent Host Binds):**
* Сформировать структуру базового конфигурационного файла, разбитую на логические блоки (Project Environment, Data Directories, POSTGRESQL, SQLAlchemy, REDIS, RABBITMQ).
* Настроить переменные путей (`PROJECT_DATA_DIR`, `POSTGRES_DATA_DIR="${PROJECT_DATA_DIR}/db"`, `REDIS_DATA_DIR="${PROJECT_DATA_DIR}/redis"`, `RABBITMQ_DATA_DIR="${PROJECT_DATA_DIR}/rabbitmq"`, `LOGS_DATA_DIR="${PROJECT_DATA_DIR}/logs"`), чтобы они динамически формировались на основе базового пути хоста для обеспечения персистентности данных.


2. **Настройка единого `docker-compose.yml`:**
* Описать сервисы (PostgreSQL, Redis, RabbitMQ, db_update, master_bot, bot_worker) в одном файле `docker-compose.yml`, который будет использоваться **и для разработки, и для тестов**.
* Для монтирования data-директорий строго использовать расширенный синтаксис `type: bind`.
* Внедрить обязательную валидацию переменных окружения: `source: ${POSTGRES_DATA_DIR:?POSTGRES_DATA_DIR не задан — скопируйте .env.example в .env и укажите каталог данных}` и `source: ${LOGS_DATA_DIR:?LOGS_DATA_DIR не задан — скопируйте .env.example в .env и укажите каталог данных}`.
* Bind `${LOGS_DATA_DIR}` → `/app/logs` монтируется **в сервисы `master_bot` и `bot_worker`** (оба пишут файловые логи Click-log по ТЗ п. 47.3–47.4); в остальные сервисы монтирование не добавляется.
* Bind `${LOGS_DATA_DIR}` → `/app/logs` монтируется в `master_bot` и `bot_worker` одинаковым путём — файловые логи обоих сервисов попадают в один каталог хоста.


3. **Генерация тестового окружения (`.env.test`):**
* Написать команду для `Makefile`, которая автоматически создает/обновляет файл `.env.test` на основе локального `.env`.
* При генерации скрипт должен заменять значение `PROJECT_ENVIRONMENT=loc` на `PROJECT_ENVIRONMENT=test`.
* Скрипт должен изменять директорию данных (например, `/data/tg_shop_platform` ➔ `/data/tg_shop_platform_test`), чтобы изолировать тестовые базы данных от локальных.
* Скрипт должен подменять боевые ключи внешних API (например, платежных шлюзов) на тестовые заглушки (mock-значения).


4. **Разработка `Makefile` (Оркестрация):**
* Реализовать цель `make init-dirs`: создание структуры директорий на хосте под bind-монтирование, включая `${LOGS_DATA_DIR}` (каталог файловых логов, монтируется в `master_bot` и `bot_worker`).
* Реализовать цель `make env-test`: автоматическая генерация файла `.env.test` из `.env` (через `sed` или `awk`).
* Реализовать цель `make up`: запуск проекта для локальной разработки (`docker compose up -d`).
* Реализовать цель `make down`: остановка проекта для локальной разработки (`docker compose down`).
* Реализовать цель `make test-integration`: запуск интеграционных тестов в изолированном окружении (`make env-test`, затем `docker compose --env-file .env.test up --build --abort-on-container-exit`).
* Реализовать цель `make test-integration-down`: Снести тестовый стек (down -v); PURGE=1 — удалить и $(TEST_DATA_DIR).
* Реализовать цель `make clean`: удаление контейнеров, сетей и тестовых директорий (`docker compose --env-file .env.test down -v` и `rm -rf /data/tg_shop_platform_test`).
* Реализовать цель `make test`: запуск unit-тесты на хосте (без внешних сервисов).
* Реализовать цель `make install`: Установить все зависимости монорепозитория через uv и pre-commit.
* Реализовать цель `make lint`: Проверить код линтером Ruff и MyPy.
* Реализовать цель `make format`: Отформатировать код через Ruff.
* Реализовать цель `make check`: Полный прогон: форматирование, линтинг и тесты.

---

### Базовые абстракции (Shared Kernel)

Все доменные сущности и SQLAlchemy-модели наследуются от базового класса, который реализует:

* `created_at`, `updated_at`: `DateTime` (Автоматическое проставление таймстемпов).

Все бизнес сущности и SQLAlchemy-модели наследуются от базового бизнес класса, который наследуется от базового класса и реализует:

* `is_active`: `Boolean` (Флаг активности, по умолчанию `True`).
* `is_deleted`: `Boolean` (Мягкое удаление, по умолчанию `False`). Физическое удаление (`DELETE` запрос) в проекте запрещено. На уровне репозиториев внедряется глобальный фильтр `where(is_deleted == False)`.

---

### Системные параметры, Кэширование полномочий и Оптимизация IAM

**Цель:** Разработать модуль управления глобальными настройками платформы с использованием плоской структуры базы данных, а также внедрить паттерн кэширования Cache-Aside (PostgreSQL ➔ Redis) для молниеносной проверки ролей в `Master Bot`.

**Задачи для AI-ассистента:**

1. **Создание плоской модели `SystemSettings` (PostgreSQL):**
* Сгенерировать SQLAlchemy-модель `SystemSettings`, наследуемую от `BaseEntity`.
* Реализовать паттерн Singleton на уровне БД: жестко зафиксировать `id=1`, отключить автоинкремент (`autoincrement=False`) и добавить ограничение `CheckConstraint("id = 1")`.
* Добавить строго типизированные поля конфигурации:
* `is_maintenance_mode` (Boolean, default=False) — режим технических работ.
* `iam_cache_ttl_seconds` (Integer, default=60*60*24) — время хранения прав пользователя в Redis.
* `extra_config` (JSONB) — резервное поле для мелких фиче-тогглов без миграций.
* `user_activity_retention_months` (Integer, NOT NULL, default=3) + `CheckConstraint('user_activity_retention_months BETWEEN 1 AND 120')` — срок хранения Click-log; источник истины для `partman.part_config.retention` таблицы `public.user_activity` (см. раздел «Партиционирование логов»).
* `audit_logs_retention_months` (Integer, NOT NULL, default=24) + `CheckConstraint('audit_logs_retention_months BETWEEN 1 AND 120')` — срок хранения Audit Log до detach в схему `archive`; источник истины для `partman.part_config.retention` таблицы `public.audit_logs` (см. раздел «Партиционирование логов»).


* Подготовить `UPSERT`-запрос для первоначальной инициализации `SystemSettings` при старте платформы (`setup.py`).


2. **Архитектура кэширования полномочий (Redis Cache-Aside):**
* Определить строгую структуру ключей в Redis, чтобы избежать коллизий: использовать формат `user:{telegram_user_id}:permissions`.
* Разработать логику чтения (Read-Through): при проверке прав сначала выполнять `GET` запрос к Redis. В случае промаха (Cache Miss) — выгружать права из PostgreSQL, парсить плоский список `{module}.{action}` в `set[str]`, записывать в Redis через `SETEX` с использованием системного параметра `iam_cache_ttl_seconds` и возвращать результат.


3. **Реализация Auth Middleware (Master Bot):**
* Написать `aiogram`-мидлварь, которая перехватывает каждое действие пользователя (сообщение или callback).
* Внутри мидлвари инкапсулировать вызов логики Cache-Aside (описанной в п. 2).
* Внедрить извлеченный `set[str]` полномочий и объект пользователя в контекст IoC-контейнера (`Dishka`), чтобы гранулярные права (например, `products.create` или `tenants.view_all`) были мгновенно доступны в обработчиках и геттерах `aiogram-dialog`.


4. **Инвалидация кэша при изменении ролей (Cache Invalidation):**
* Разработать/обновить юзкейс (UseCase) в модуле IAM, отвечающий за назначение или снятие ролей.
* Внедрить жесткое правило: сразу после фиксации транзакции в БД (`session.commit()`) выполнять прямое удаление ключа в Redis: `await redis.delete(f"user:{user_id}:permissions")`. Никакого Pub/Sub для IAM использовать не нужно, так как проверка и изменение происходят в рамках Master Bot.

---

### Структура Базы Данных (ER-модель)

* Все текстовые поля указываются без размера.
* Индексы для всех полей FK
* В бизнес-таблицах обязателен `tenant_id`.

**Модуль IAM (Динамический RBAC Конструктор):**

1. `permissions`: Справочник полномочий платформы.
* `id` (PK)
* `name`: String (Наименование).
* `slug`: (Unique, String) (Формат `{module}.{action}`, например: `products.create`, `cases.refund`, `tenants.view_all`).


2. `roles`: Роли.
* `id` (PK)
* `name`: String (Наименование).
* `slug` (Unique, String)
* `is_active`, `is_deleted`: Boolean.


3. `role_permissions`: Связь роли и её полномочий.
* `role_id` (FK), `permission_id` (FK).


4. `users`: Глобальные аккаунты.
* `id` (PK), `telegram_user_id` (Unique, BigInt), `telegram_user_name` (String).
* `is_superuser`: Boolean (Абсолютный владелец платформы, игнорирует RBAC).
* `is_active`, `is_deleted`: Boolean.


5. `user_roles`: Связь пользователя и ролей (у юзера может быть несколько ролей).
* `user_id` (FK), `role_id` (FK).

6. `languages`: Список доступных языков
* `code` (String), name (String), `is_default`: (Boolean, default=false).
* `is_active`, `is_deleted`: Boolean.
* *Логика:* `is_default=true` может быть только у одной записи в таблице

7. `tenants`: Магазины.
* `id` (uuid), `name` (String), `slug` (Unique, String).
* `is_active`, `is_deleted`: Boolean.


8. `tenant_settings`: Настройки магазинов.
* `id` (uuid), tenant_id (Unique, uuid), is_store_open (boolean), is_buy_enabled (boolean), is_refill_enabled (boolean), is_referral_enabled (boolean), is_notifications_enabled (boolean),
default_language_code (String), support_chat_id (bigint), news_chat_id (bigint), faq (jsonb), extra (jsonb).


9. `tenant_moderators`: Привязка пользователя к магазину (Scope).
* `user_id` (FK), `tenant_id` (FK).
* *Логика:* Если у роли пользователя есть полномочие `tenants.view_all` (аналог бывшего `senior_moderator`), он имеет доступ ко всем магазинам. В противном случае доступ проверяется по этой таблице.



**Модуль логирования (User Activity & Audit Log) — партиционированные таблицы:**

10. `user_activity`: Click-log / User Activity (ТЗ п. 47.1–47.2, 47.10). *Партиционирована RANGE(`created_at`), месячные партиции (pg_partman).*
* Composite PK: `(id uuid, created_at TIMESTAMPTZ)`.
* `tenant_id` (uuid), `bot_id` (uuid), `telegram_user_id` (BigInt), `username` (String, на момент события), `user_type`/`role` (String).
* `source` (String): `callback` / `message` / `command` / `reply_keyboard` / `media` / `admin` — тип источника события. Обоснование: ТЗ п. 47.1 требует логировать действия через команды, текст, reply/inline keyboard и media; без этого поля невозможна фильтрация по типу источника (ТЗ п. 47.6).
* `action_type` (String): Enum/String с валидацией против справочника — значения по ТЗ п. 47.1 (inline/reply-кнопки, переходы категория/подкатегория, открытие позиции, просмотр товара/профиля, заказ, покупка, отмена, предзаказ, отзыв, обращение, сообщения, ответы, возвраты, кнопки оплаты, пополнение, меню, «Назад», «Главное меню», подписка на уведомления, рефералы, действия модератора, админ-действия, ошибки flow).
* `entity_type` (String), `entity_id` (uuid), `order_id`, `product_id`, `category_id`, `subcategory_id`, `case_id`, `preorder_id` (uuid, nullable).
* `metadata` (JSONB, только безопасные данные без секретов/PII), `result` (String: `success` / `error` / `cancelled`).
* Индексы: `(tenant_id, created_at)`, `(telegram_user_id, created_at)`, `(bot_id, created_at)`.
* FK на `id` не создаются — ссылки на другие сущности только логические через `entity_type`/`entity_id`.


11. `audit_logs`: Audit Log — только критические действия (ТЗ п. 47.7, 47.9). *Партиционирована RANGE(`created_at`), месячные партиции (pg_partman).*
* Composite PK: `(id uuid, created_at TIMESTAMPTZ)` — требование нативного партиционирования PostgreSQL (по остальным таблицам логов — аналогично).
* `tenant_id` (uuid), `bot_id` (UUID, nullable — контекст бота в мульти-бот платформе, ТЗ п. 47.10), `actor_id` (uuid/BigInt, инициатор действия).
* `action` (String), полный перечень значений по ТЗ п. 47.7: `write_off_moderator`, `write_off_client`, `product_issue`, `product_replace`, `refund`, `payout_approve`, `payout_reject`, `balance_change`, `permission_change`, `role_change`, `product_change`, `price_change`, `discount_change`, `settings_change`, `admin_action`.
* `entity_type` (String), `entity_id` (uuid), `payload` (JSONB), `result` (String).
* Индексы: `(created_at)`, `(tenant_id, created_at)`.
* *Логика:* Click log не заменяет AuditLog (ТЗ п. 47.7); ссылки на сущности — логические, FK нет.
* *Типы времени:* в ER-блоках логов `created_at` — `TIMESTAMPTZ` (соответствует `DateTime(timezone=True)` в SQLAlchemy). Все значения пишутся в UTC; отображение в таймзоне пользователя — задача админки (ТЗ п. 47.2).


**Остальные модули (Catalog, BotManager, Orders, Marketing, Support):**
*Все таблицы (`bots`, `categories`, `products`, `orders`, `tickets` и т.д.) получают поля `created_at, updated_at, is_active, is_deleted` из Shared Kernel.*

---

### Партиционирование логов (PostgreSQL 17 + pg_partman)

**Назначение.** Две независимые лог-таблицы, обе нативно партиционированы методом RANGE по `created_at` помесячно:

* `user_activity` — Click-log / User Activity (ТЗ п. 47.1–47.2, 47.10): наиболее объёмная таблица платформы, пишется практически на каждое действие пользователя.
* `audit_logs` — Audit Log (ТЗ п. 47.7, 47.9): только критические действия (refund, payout approval/rejection, изменение баланса, списания, изменение permissions/ролей/товаров/цен/скидок/настроек, административные действия).
* Жесткое правило из ТЗ (п. 47.7): **«Click log не заменяет AuditLog»** — это две разные таблицы с разной глубиной хранения.

**Состав полей.** Полный перечень полей — см. таблицы 10–11 ER-модели (выше). Специфичные для партиционирования акценты:

* Ключ партиционирования — `created_at` (`TIMESTAMPTZ`, NOT NULL) и входит в Composite PK `(id, created_at)`.
* Обязательные дискриминаторы для фильтрации и изоляции тенантов: `tenant_id` и `bot_id` (`bot_id` — nullable только в `audit_logs`) — входят в индексы вместе с `created_at`.

**Составной первичный ключ (требование нативного партиционирования).**

* Обе таблицы: `PRIMARY KEY (id UUID, created_at TIMESTAMPTZ)` — PostgreSQL требует, чтобы ключ партиционирования входил в PK; `created_at` объявляется как `DateTime(timezone=True)`, NOT NULL. TIMESTAMPTZ, а не TIMESTAMP: все события хранятся в UTC, отображение в локальной таймзоне — в админке (ТЗ п. 47.2).
* Следствие: FOREIGN KEY из других таблиц на `id` лога **не создаётся** (нельзя ссылаться на неуникальный по отдельности столбец); ссылки — только логические через `entity_type` / `entity_id` (и `order_id`, `product_id` и т.д.).

**Инфраструктурный слой.**

* `ops/postgres/Dockerfile` на базе `postgres:17` (Debian-образ) с установкой пакета расширения одним слоем:
```dockerfile
FROM postgres:17
RUN apt-get update && apt-get install -y postgresql-17-partman \
    && rm -rf /var/lib/apt/lists/*
```
* В `docker-compose.yml` сервис БД переводится с `image:` на `build: ./ops/postgres`. Смена `image:` на `build: ./ops/postgres` не меняет монтирование данных: bind-маунт `${POSTGRES_DATA_DIR}` → `/var/lib/postgresql/data` сохраняется; те же переменные (`PROJECT_DATA_DIR` / `POSTGRES_DATA_DIR`) используются в `make clean` и `make test-integration` (`.env.test`) — пересборка образа не трогает данные на хосте.
* В `command` сервиса БД добавляются параметры фонового воркера pg_partman:
```yaml
command:
  - "postgres"
  - "-c"
  - "shared_preload_libraries=pg_partman_bgw"
  - "-c"
  - "pg_partman_bgw.interval=3600"
  - "-c"
  - "pg_partman_bgw.role=${POSTGRES_USER}"
  - "-c"
  - "pg_partman_bgw.dbname=${POSTGRES_DB}"
```
* Имена роли и БД берутся только из `.env` (`POSTGRES_USER`, `POSTGRES_DB`); в коде и конфигах не хардкодятся.
* **Права роли.** Миграции `db_update` и `pg_partman_bgw.role` используют одну и ту же роль `${POSTGRES_USER}`. Эта роль — владелец таблиц логов и схемы `partman`: иначе `run_maintenance()` не сможет создавать/обслуживать партиции (требование документации pg_partman к не-суперпользователям — владелец родительской таблицы должен совпадать с ролью BGW). Суперпользователь для этого не требуется.

**Слой приложения (SQLAlchemy 2.0).**

* Модели `UserActivity` и `AuditLog` размещаются в инфраструктурном слое соответствующих модулей (`app/modules/<module>/infrastructure/models.py`). Модуль-владелец логов определяется при реализации: либо отдельный bounded context (`logging` / `audit`), либо размещение в `app/shared_kernel/infrastructure` (логи используются всеми модулями).
* `__table_args__` моделей обязательно содержит `{"postgresql_partition_by": "RANGE (created_at)"}` плюс индексы:
* `user_activity`: `(tenant_id, created_at)`, `(telegram_user_id, created_at)`, `(bot_id, created_at)`.
* `audit_logs`: `(created_at)`, `(tenant_id, created_at)`.
* `created_at` — `DateTime(timezone=True)`, NOT NULL (ключ партиционирования и часть Composite PK).

**Слой миграций (Alembic).**

* `alembic revision --autogenerate` не умеет генерировать партиционирование и расширения, поэтому миграция создаётся пустой (`alembic revision -m "partition logs"`), а вся логика пишется через `op.execute(...)`.
* `upgrade()`:
```sql
CREATE SCHEMA IF NOT EXISTS partman;
CREATE SCHEMA IF NOT EXISTS archive;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;

SELECT partman.create_parent(
    p_parent_table := 'public.<table>',
    p_control      := 'created_at',
    p_interval     := '1 month',
    p_type         := 'range',
    p_premake      := 3
);

UPDATE partman.part_config
   SET retention = '<N> months',
       retention_keep_table = <bool>,
       infinite_time_partitions = <bool>
 WHERE parent_table = 'public.<table>';
```
* **Дефолты `create_parent`.** Обязательные параметры без дефолта: `p_parent_table` (schema-qualified), `p_control`, `p_interval`. Остальные имеют дефолты: `p_type` DEFAULT `'range'`, `p_premake` DEFAULT `4`, `p_default_table` DEFAULT `true`, `p_automatic_maintenance` DEFAULT `'on'`. Поэтому в вызове значения передаются явно там, где нужно не-дефолтное поведение (`p_type := 'range'`, `p_premake := 3`), а `p_default_table` и `p_automatic_maintenance` не передаются — используются дефолты (`true` — INSERT вне диапазона партиций не падает, а попадает в DEFAULT-партицию; `'on'` — обслуживание через `run_maintenance()`/BGW включается *автоматической* поддержкой pg_partman).
* **Обоснование `p_premake := 3`.** Трёх месяцев запаса достаточно при работающем BGW и мониторинге (метрика в Этапе 2, «Наблюдаемость»). Если BGW падает дольше этого срока, INSERT уйдёт в DEFAULT-партицию и потребует ручного разбора — поэтому BGW-воркер обязательно попадает в алертинг (контроль `pg_partman_bgw` и наличия будущих партиций).
* `downgrade()`:
```sql
-- 1) создать целевую непартиционированную таблицу (INCLUDING ALL -> DEFAULT/NOT NULL/CHECK)
CREATE TABLE IF NOT EXISTS public.<table>_undo (LIKE public.<table> INCLUDING ALL);
-- 2) снять партиционирование, перенеся данные в target (один батч)
SELECT partman.undo_partition(
    p_parent_table   := 'public.<table>',
    p_target_table   := 'public.<table>_undo',
    p_loop_count     := 1,
    p_batch_interval := '1 day',
    p_keep_table     := false
);
-- 3) вернуть прежнее имя
DROP TABLE public.<table>;
ALTER TABLE public.<table>_undo RENAME TO <table>;
-- 4) восстановить PK и индексы (INCLUDING ALL не переносит констрейнты родителя)
ALTER TABLE public.<table> ADD CONSTRAINT <table>_pk PRIMARY KEY (id, created_at);
CREATE INDEX ix_<table>_tenant_created        ON public.<table> (tenant_id, created_at);
CREATE INDEX ix_<table>_telegram_user_created ON public.<table> (telegram_user_id, created_at); -- только user_activity
CREATE INDEX ix_<table>_bot_created           ON public.<table> (bot_id, created_at);           -- только user_activity
CREATE INDEX ix_<table>_created_at            ON public.<table> (created_at);                    -- только audit_logs
```
* **Почему PK и индексы перечислены явно.** `LIKE ... INCLUDING ALL` не переносит первичный ключ, индексы и `CHECK`-констрейнты самого родителя; `undo_partition` лишь переносит строки в целевую таблицу (ограничения, созданные на целевой таблице, при переносе не появляются автоматически). Поэтому PK и все индексы из `__table_args__` соответствующей модели восстанавливаются после `RENAME`.
* **Почему это критично до возврата трафика.** `id` и `created_at` генерируются приложением (без `DEFAULT` на уровне БД, `id` — uuid4 из Python), а `created_at` объявлен NOT NULL. Без восстановленных PK/NOT NULL-ограничений таблица принимает вставки, но теряет уникальность и целостность логов — поэтому PK и ограничения создаются до открытия трафика.
* **`p_loop_count := 1`, а не 1000.** `p_loop_count` — число батчей по `p_batch_interval`; `1000` батчей по 1 дню — многолетняя транзакция. Один батч на прогон. Для больших объёмов (много дочерних партиций) документация pg_partman советует использовать процедуру `partman.undo_partition_proc` (та же сигнатура параметров, вызывается через `CALL`), запускаемую в обслуживаемое окно.
* `DELETE FROM partman.part_config` перед `undo_partition` **не выполняется**: функция требует, чтобы `parent_table` уже был сконфигурирован (иначе ошибка «parent_table must match a parent table already configured»), и при успешном завершении сама удаляет запись из `part_config`. Партиционированную таблицу нельзя превратить в обычную на месте — поэтому данные переносятся в отдельную целевую таблицу (`p_target_table` — обязательный второй аргумент).
* ⚠️ `undo_partition` — тяжёлая операция (перенос всех строк таблицы в новую, обычную таблицу) и на больших объёмах блокирует запись. Для продакшена downgrade не является штатным путём отката — откат выполняется восстановлением из бэкапа (см. Этап 8). Таблицы, перемещённые в схему `archive` по `retention_schema`, `downgrade()` не затрагивает — они вне планшета pg_partman; их обработка ручная (выгрузка в MinIO / ручное удаление).

**Retention policy (настраиваемый срок хранения).**

Сроки хранения **не хардкодятся** — ТЗ п. 47.9: «Конкретный срок хранения определить в настройках проекта». Реализация в модели `SystemSettings` (см. раздел «Системные параметры, Кэширование полномочий и Оптимизация IAM») — добавляются новые поля:

* `user_activity_retention_months` (Integer, NOT NULL, default=3) + `retention_keep_table = false` → старые партиции click-log физически удаляются (`DROP TABLE`). Значение валидируется `CheckConstraint('... BETWEEN 1 AND 120')`; `retention_keep_index` для этой таблицы не применяется (партиция дропается целиком).
* `audit_logs_retention_months` (Integer, NOT NULL, default=24) + `retention_schema = 'archive'` (см. ниже) → старые партиции audit лога **отвязываются (detach) и перемещаются в схему `archive`, но не удаляются**; их контролируемое удаление — процедурой архивации (`ops/scripts/archive_audit_partitions.py`, см. ниже). Обоснование: ТЗ п. 47.7 требует не терять информацию о критических действиях, ТЗ п. 34 требует бэкап, а audit-лог — источник аудита.
* `SystemSettings` (оба поля): `CheckConstraint('... BETWEEN 1 AND 120')` на `user_activity_retention_months` и `audit_logs_retention_months` — диапазон 1..120 месяцев валидируется на уровне БД.

**Обоснование значений retention (не «магические числа»).**

* `user_activity` — 3 месяца: объём пропорционален числу действий пользователей (пишется практически на каждый клик), а историческая ценность отдельного клика низкая — агрегаты живут в статистике/аналитике; глубокий архив кликов не нужен.
* `audit_logs` — 24 месяца: консервативный горизонт финансового/налогового аудита (2 года). Значение осознанно выше минимально необходимого и меняется через `SystemSettings` без миграции.
* Точная оценка объёма строк/мес по каждой таблице делается на этапе эксплуатации (по фактическим метрикам `pg_total_relation_size` партиций) и корректируется через настройки.

**Архив audit-партиций (как именно обеспечивается).**

* При `retention_keep_table = true` партиция остаётся в **той же схеме** (только detach). Перенос в другую схему делает только параметр `retention_schema`, и при его задании он переопределяет `retention_keep_table`.
* Конфигурация в `upgrade()`/синхронизации:
```sql
CREATE SCHEMA IF NOT EXISTS archive;

-- audit_logs: архивная схема
UPDATE partman.part_config
   SET retention          = '<N> months',
       retention_schema   = 'archive',
       retention_keep_index = true,
       infinite_time_partitions = true
 WHERE parent_table = 'public.audit_logs';

-- user_activity: физическое удаление
UPDATE partman.part_config
   SET retention          = '<N> months',
       retention_keep_table = false,
       infinite_time_partitions = false
 WHERE parent_table = 'public.user_activity';
```
* Для `audit_logs` `retention_keep_table` **не выставляется** — при заданном `retention_schema` он игнорируется. Оговорка: если `retention_schema` по каким-то причинам не задан, то для audit-лога обязателен `retention_keep_table = true` (иначе партиции будут удалены).
* `retention_keep_index = true` сохраняет индексы у detach-нутых партиций (нужно для разбора инцидентов по старому периоду); без него архивные партиции остались бы без индексов.
* `retention_keep_index` для `user_activity` **не применяется**: партиция дропается целиком (`retention_keep_table = false`) вместе со всеми своими индексами, отдельно сохранять их не у чего; параметр имеет смысл только для `audit_logs` (detach без drop).
* `infinite_time_partitions` различается по таблицам: `infinite_time_partitions = true` для `audit_logs` — партиции продлеваются вперёд даже при отсутствии событий; защита от ранней записи обеспечивается `p_premake := 3` и наличием DEFAULT-партиции. `false` для `user_activity` (поток непрерывный, партиция создаётся при первой вставке).

> **Жёсткое правило:** для `public.audit_logs` запрещено выставлять `retention_keep_table = false` и запрещено сбрасывать `retention_schema` в NULL. Удаление партиций audit-лога — только процедурой архивации с предварительной выгрузкой в MinIO.

**Guard: техническая реализация жёсткого правила.**

* Скрипт синхронизации: `ops/scripts/sync_partman_retention.py` (Python, запускается шагом `db_update` после `alembic upgrade head`).
* Перед `UPDATE` скрипт выполняет блокирующую проверку текущего состояния строки конфига audit-лога:
```sql
SELECT retention_keep_table, retention_schema
  FROM partman.part_config
 WHERE parent_table = 'public.audit_logs'
   FOR UPDATE;
```
  Если `retention_keep_table = false` **или** `retention_schema IS NULL` — скрипт бросает исключение (`PartmanRetentionGuardError`) и завершает работу с ненулевым кодом, **не записывая** значения из `SystemSettings` (никаких частичных UPDATE по другим таблицам после этого — транзакция откатывается целиком).
* Вторая линия защиты — на уровне БД: `BEFORE UPDATE` триггер на `partman.part_config` для `parent_table = 'public.audit_logs'`, который делает `RAISE EXCEPTION`, если в `NEW` выставлено `retention_keep_table = false` или `retention_schema = NULL`:
```sql
CREATE OR REPLACE FUNCTION public.tg_guard_audit_part_config()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_table = 'public.audit_logs'
       AND (NEW.retention_keep_table IS FALSE OR NEW.retention_schema IS NULL) THEN
        RAISE EXCEPTION 'audit_logs: retention_keep_table=false / retention_schema IS NULL запрещены';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER guard_audit_part_config
    BEFORE UPDATE ON partman.part_config
    FOR EACH ROW EXECUTE FUNCTION public.tg_guard_audit_part_config();
```
* Оговорка о правах: скрипт и BGW работают под `${POSTGRES_USER}` — владельцем схемы `partman`, поэтому ACL-защиты (запрета на `UPDATE partman.part_config`) нет; триггер — единственная защита от ручного `UPDATE` в обход скрипта.

**Применение значений из настроек.** Скрипт `ops/scripts/sync_partman_retention.py` читает `SystemSettings` и обновляет `partman.part_config` при выполнении `db_update` (после `alembic upgrade head`), точным запросом:
```sql
UPDATE partman.part_config pc
   SET retention = (s.<field>::text || ' months')
  FROM public.system_settings s
 WHERE pc.parent_table = 'public.<table>' AND s.id = 1;
```
* Пример для `user_activity`:
```sql
UPDATE partman.part_config pc
   SET retention = (s.user_activity_retention_months::text || ' months')
  FROM public.system_settings s
 WHERE pc.parent_table = 'public.user_activity' AND s.id = 1;
```
* колонка `retention` — text, значение кастуется к interval при обслуживании. В миграции допускается значение по умолчанию; итоговое — из настроек.
* **Момент применения.** Значения применяются только при прогоне `db_update` (скрипт синхронизации); изменение настроек в UI само по себе партиции не перенастраивает — запись в `system_settings` меняется, а `partman.part_config` остаётся прежним до следующего деплоя/прогона. Для немедленного применения предусмотреть admin-действие «пересчитать retention», вызывающее тот же скрипт `ops/scripts/sync_partman_retention.py`.
* **Строгий формат значения.** Строка формируется как `f"{value} months"` при `value` целочисленном из диапазона 1..120 (тот же диапазон, что в `CheckConstraint` модели). Валидация двойная: `CheckConstraint` в `SystemSettings` и формат `^[0-9]+ months$`. Опционально в миграции можно добавить на `partman.part_config` ограничение `CONSTRAINT chk_retention_format CHECK (retention IS NULL OR retention ~ '^[0-9]+ months$')` — только для наших двух `parent_table` (`public.user_activity`, `public.audit_logs`), т.к. таблица общая для всех партиционированных родителей.

**Архив audit-партиций: контроль роста схемы `archive`.**

* Партиции, перемещённые в `archive` (`retention_schema = 'archive'`), выпадают из планшета pg_partman — policy retention на них больше не действует, и без отдельной процедуры схема `archive` растёт бесконечно. ТЗ п. 47.9 прямо требует «нельзя допускать бесконтрольного роста», поэтому нужна периодическая процедура `ops/scripts/archive_audit_partitions.py` (запуск по cron на хосте или Makefile target, например `make archive-audit`):
  1. найти в схеме `archive` партиции вида `audit_logs_pYYYY_MM` старше заданного порога (по имени/`pg_class`);
  2. выгрузить каждую партицию в MinIO (`storage_key` вида `audit/<table>_pYYYY_MM/`, файл на партицию + манифест с числом строк и контрольной суммой);
  3. **только после успешной выгрузки** выполнить `DROP TABLE archive.<table>_pYYYY_MM`. При ошибке выгрузки партиция остаётся — повторный прогон идемпотентен.
* ⚠️ **Уникальность имени партиции.** При `retention_schema = 'archive'` имя партиции `<table>_pYYYY_MM` должно быть уникальным в схеме `archive`: если таблица с таким именем там уже есть (следствие ручного отката `downgrade()`/`RENAME` или повторного detach того же периода), pg_partman упадёт с `relation already exists`. Процедура архивации обязана проверять наличие такой таблицы заранее и удалять/выгружать её до следующего detach; в идеале — проверка выполняется и в `sync_partman_retention.py` как часть guard-логики.

**Разделение ответственности логов.**

* `user_activity` → PostgreSQL + текстовые файлы (ТЗ п. 47.3) + bind-маунт каталога `LOGS_DATA_DIR` (`.env`: `LOGS_DATA_DIR="${PROJECT_DATA_DIR}/logs"`) (ТЗ п. 47.4) + ротация файлов (ТЗ п. 47.9). Bind `${LOGS_DATA_DIR}` → `/app/logs` подключается в сервисы `master_bot` и `bot_worker`; каталог создаётся целью `make init-dirs`.
* `audit_logs` → только PostgreSQL + выгрузка в архив (MinIO / схема `archive`) при detach старых партиций.
* Запись логов асинхронная/буферизированная: ошибка логирования никогда не ломает покупку, оплату или иную основную операцию (ТЗ п. 47.8). Запись идёт через `LogBuffer` — asyncio-очередь с ограниченным размером; при переполнении или ошибке записи события сбрасываются в структурный stderr-лог и инкрементируется метрика `logs_dropped_total`; основной flow (покупка/оплата) не блокируется. При необходимости буфер заменяется на публикацию в RabbitMQ/FastStream без изменения вызывающего кода (интерфейс логгера — порт, реализация — адаптер).

**Тестирование (ТЗ п. 36).**

* Тест на наличие партиций на N месяцев вперёд (`partman.part_config` + фактические `pg_class`-партиции).
* Тест, что `INSERT` по `created_at` попадает в нужную партицию, включая граничные значения месяца (последняя секунда месяца / первая секунда следующего).
* Тест, что сбой логгера не ломает основную операцию — ТЗ п. 47.8 (логирование с ошибкой / недоступная таблица → покупка завершается успешно).
* Тест миграции `upgrade()` на чистой БД (создаётся расширение, схема `partman`, родительские таблицы и партиции).
* Тест на отсутствие «сиротских» партиций в схеме `archive`: проверка через `pg_class` по схеме `archive` не должно быть таблиц `audit_logs_pYYYY_MM` старше порога (N месяцев + запас на процедуру архивации) — гарантирует, что `ops/scripts/archive_audit_partitions.py` реально запускается (ТЗ п. 47.9).
* Тест downgrade (`downgrade()`): после `RENAME` у таблицы есть PK `(id, created_at)` и все индексы из `__table_args__`; `INSERT` дубликата `id` падает на PK.

**Что входит в roadmap.** pg_partman (самодельный Docker-образ БД), партиционированные таблицы `user_activity` / `audit_logs` и их модели входят в Этап 1 (инфраструктурный фундамент + слой хранения). Детали логов (файловые логи в `LOGS_DATA_DIR`, просмотр в админке, фильтры, экспорт TXT по ТЗ п. 47.6) — в этап реализации раздела «Click log / User Activity» и в этап Telegram Admin.
* В этап **Telegram Admin** обязательно входит просмотр `user_activity` с фильтрами по ТЗ п. 47.6 (пользователь, Telegram ID, username, bot, tenant, дата/диапазон, тип действия, product, category, subcategory, order, case, preorder, moderator), сортировка и экспорт в TXT.

---

### План разработки для AI-ассистента (Roadmap)

Генерация кода выполняется строго по этапам. Обязательна реализация Soft Delete на уровне `BaseRepository`.

#### Этап 1. Фундамент, Soft Delete и Динамический RBAC

* docker-compose.yml для кластера платформы.
* **Логи и партиционирование:** образ `ops/postgres` на базе `postgres:17` с `postgresql-17-partman`, параметры `pg_partman_bgw` в `command` сервиса БД, таблицы `user_activity` / `audit_logs` (RANGE по `created_at`, месячные партиции), пустая миграция с `op.execute(...)`, схемы `partman`/`archive`, guard-триггер на `partman.part_config`, поля retention в `SystemSettings`, скрипты `ops/scripts/sync_partman_retention.py` и `ops/scripts/archive_audit_partitions.py`, bind `${LOGS_DATA_DIR}` → `/app/logs` в `master_bot`/`bot_worker`.
* **Shared Kernel:** Написание `BaseEntity` и базовой SQLAlchemy модели с полями `is_active`, `is_deleted`, `created_at`, `updated_at`. Реализация `BaseRepository` с автоматической фильтрацией удаленных записей и методом `soft_delete()`.
* **Схема БД:** Модели IAM (`users`, `roles`, `permissions`, `role_permissions`, `user_roles`, `tenants`, `tenant_settings`, `tenant_moderators`).
* **Логика RBAC:** Сервисный слой, который принимает `telegram_user_id` и `tenant_id`, агрегирует все `permissions` из всех ролей пользователя, проверяет флаги `is_active` и выдает вердикт (например, `auth_service.has_permission(user_id, "products.create", tenant_id)`).
* **Alembic:** Интегрировать Alembic для управления схемой PostgreSQL, адаптировав его под современный драйвер `psycopg` (v3) в асинхронном режиме, модульный монолит и уже существующий в инфраструктуре сервис применения миграций `db_update`.

1. **Настройка Naming Conventions (SQLAlchemy Base):**
* В файле `app/shared_kernel/infrastructure/database.py` определить кастомный объект `MetaData` со строгими правилами именования (Naming Conventions) для индексов (ix), уникальных ограничений (uq), проверок (ck), внешних ключей (fk) и первичных ключей (pk).
* Привязать этот `MetaData` к базовому классу декларативных моделей (`Base`). Это критически важно, чтобы Alembic корректно генерировал имена ограничений и мог их безопасно удалять при даунгрейдах.


2. **Адаптация `migrations/env.py` под `psycopg` и Модульный Монолит:**
* Настроить `env.py` для работы с асинхронным драйвером базы данных (`postgresql+psycopg://`) через `asyncio.run()`.
* Реализовать блок явных импортов моделей из всех Bounded Contexts (модулей) проекта (например, `import app.modules.iam.infrastructure.models`, `import app.modules.catalog.infrastructure.models` и т.д.).
* Передать `Base.metadata` из `shared_kernel` в переменную `target_metadata`, чтобы Alembic автоматически "увидел" структуру всех модулей и смог генерировать единую ленту ревизий (`--autogenerate`).


3. **Интеграция с конфигурацией окружения (`.env`):**
* Настроить `app.core.config` (через `pydantic-settings`) таким образом, чтобы формировался корректный URL для подключения: `postgresql+psycopg://{user}:{password}@{host}:{port}/{db}`.
* Убедиться, что `alembic.ini` не содержит захардкоженного URL, а `env.py` динамически получает его из глобальной конфигурации приложения.


4. **Интеграция с сервисом `db_update` (CI/CD):**
* В каталоге `ops/scripts/` разработать entrypoint-скрипт (например, `migrate.sh`), который будет выполняться внутри сервиса `db_update`. Скрипт должен использовать `pg_isready` для ожидания доступности PostgreSQL, после чего выполнять команду `alembic upgrade head`.
* В `docker-compose.yml` настроить сервисы `master_bot` и `bot_worker` на ожидание успешного завершения работы `db_update` (условие `condition: service_completed_successfully`).


#### Этап 2: Наблюдаемость (Distributed Observability)

**Цель:** Развернуть отказоустойчивую систему мониторинга. Основные приложения работают на основном сервере (VPS 1), а агрегация логов, метрик и трекинг ошибок — на выделенном сервере (VPS 2) для обеспечения Fault Isolation.

**Задачи для AI-ассистента:**

1.  **Структура репозитория:**
    *   Создать директорию `ops/observability` для конфигурации мониторинга.
    *   Разработать отдельный файл `docker-compose.obs.yml` для запуска на VPS 2.

2.  **Конфигурация VPS 1 (Основной сервер):**
    *   В основном `docker-compose.yml` настроить легковесный коллектор **Grafana Alloy**, который собирает JSON-логи Docker-контейнеров и метрики (Node, PG, Rabbit, App).
    *   Настроить отправку собранной телеметрии с Alloy (VPS 1) на защищенные эндпоинты Loki и Prometheus (VPS 2).
    *   В приложении (FastAPI/aiogram/FastStream) внедрить `structlog` с поддержкой `Correlation ID` (генерация `trace_id` и передача через RabbitMQ headers).
    *   Подключить `sentry_sdk`, указав внутренний DSN urgentry (VPS 2); трассировка — OpenTelemetry SDK → Alloy (OTLP) → Tempo, W3C `traceparent` через RabbitMQ. Решения и ограничения — `docs/ops/observability.md` («Трассировка»).

3.  **Конфигурация VPS 2 (Сервер Мониторинга — `docker-compose.obs.yml`):**
    *   Развернуть **Grafana Loki** для приема логов от Alloy.
    *   Развернуть **Prometheus** для агрегации метрик и настройки правил алертинга.
    *   Развернуть **Grafana** для визуализации (подключить источники Loki и Prometheus).
    *   Развернуть **urgentry** (Sentry-совместимый, один контейнер, SQLite; заменил GlitchTip) для сбора ошибок и **Tempo** (metrics-generator → Prometheus) для трейсов. Ограничения: числовой project id DSN → один проект; Service map проверяется только в UI; A-NIT-8 — веха II; сквозной трейс через ботов — после появления ботов (веха II).

4.  **Безопасность сетевого взаимодействия:**
    *   Учесть, что связь между VPS 1 и VPS 2 будет идти через защищенный VPN-туннель (WireGuard/Tailscale) по внутренним IP-адресам (например, `10.x.x.x`).
    *   Эндпоинты `/metrics` и интерфейсы Alloy/Loki не должны публиковаться на публичных интерфейсах (ограничить `ports: ["127.0.0.1:9090:9090"]` или биндить на IP VPN). В публичный интернет (на порты 80/443) выдаются только Web-интерфейсы Grafana и urgentry через reverse proxy (urgentry — на корне, под-путь не поддерживается).

#### Этап 3. Master Bot и Панель управления (aiogram-dialog)

* **Auth Middleware:** При каждом апдейте извлекает пользователя, проверяет `is_deleted == False` и `is_active == True`, внедряет в Dishka-контекст объект пользователя и плоский список его полномочий (`set[str]`).
* **RBAC Конструктор (Диалоги):**
* Интерфейс создания кастомной роли (ввод названия).
* Окно с мульти-выбором (Checkbox) полномочий (например, выбор между `products.create`, `cases.view`).
* Интерфейс назначения созданной роли пользователю.


* **Управление платформой:** Диалоги создания тенантов, запуска ботов и навигации по магазинам с учетом полномочия `tenants.view_all`.

#### Этап 4. Bot Manager & Polling Workers (Data Plane)

* Написание `bot_runner.py` (автономный слушатель RabbitMQ).
* Передача команд `START_BOT` / `STOP_BOT` из Master Bot через RabbitMQ (FastStream).
* Fail-Fast обертка поллинга: отлов `TelegramUnauthorizedError` и `TelegramRetryAfter`. Обновление статуса в БД.

#### Этап 5. Товарный контур (Catalog & Products)

* Модели каталога с наследованием от `SoftDeleteMixin`.
* Диалоги Master Bot'а для управления каталогом (загрузка фото, создание категорий).
* Изоляция данных в геттерах диалогов: показ элементов только при наличии у юзера полномочия `products.view` в контексте данного `tenant_id`.
* Интерфейс витрины (Inline-клавиатуры) для клиентского бота.

#### Этап 6. Заказы, Биллинг и Saga Outbox

* Реализация корзины покупателя.
* Transactional Outbox: создание `orders` и запись события `OrderCreated` в одной БД-транзакции.
* Saga (RabbitMQ/FastStream): списание баланса модулем Billing, публикация `OrderBilled`, обновление статуса заказа.

#### Этап 7. Маркетинг, Поддержка и Деплой

* Модули `discounts`, `lotteries`, `tickets` (с полномочиями `cases.reply`, `cases.refund`).
* CLI-инсталлятор: скрипт для заливки базовых `permissions` в таблицу справочника, создания роли `System Admin` (все полномочия) и первого пользователя `is_superuser`.

#### Этап 8: Система резервного копирования и интеллектуального восстановления (Backup & Recovery)

**Цель:** Разработать двухуровневую стратегию защиты данных: инфраструктурную (защита от физической потери сервера/БД) и прикладную (логические JSON-снимки с изоляцией по `tenant_id` для отката ошибочных мутаций данных), с гарантией сохранения финансовой иммутабельности оформленных заказов.

**Задачи для AI-ассистента:**

1. **Инфраструктурный контур (Disaster Recovery в `docker-compose.yml`):**
* **PostgreSQL:** Добавить sidecar-контейнер (например, на базе `prodrigestivill/postgres-backup-local`), подключенный к сети базы данных. Сконфигурировать переменные окружения (`S3_ACCESS_KEY`, `S3_BUCKET`, `S3_ENDPOINT` и др.) и CRON-расписание для автоматической выгрузки дампов (`pg_dump --format=custom`) в S3-совместимое хранилище.
* **Redis:** Включить гибридное сохранение состояния FSM и корзин (RDB снимки + AOF логирование) в конфигурации Redis (`appendonly yes`, `appendfsync everysec`) и убедиться, что папка смонтирована на хост через `type: bind`.


2. **Прикладной контур (Логические Snapshot-ы):**
* **Модели БД:** Реализовать SQLAlchemy-модели `Backup` (метаданные: тип, инициатор, дата, `tenant_id`) и `BackupItem` (сущности в `data: jsonb`, отсортированные по `sort_order`).
* **Асинхронная сборка (RabbitMQ/FastStream):** Реализовать генерацию бэкапа как отложенную задачу. Master Bot собирает метаданные и публикует команду `CREATE_TENANT_BACKUP` в RabbitMQ. Polling Worker в фоне собирает все данные магазина (категории, позиции, настройки), сериализует их в JSON-объекты, сохраняет через bulk-insert в `backup_items` и присылает уведомление администратору в Telegram о завершении.


3. **Умное восстановление (Smart Restore Service):**
* Разработать UseCase восстановления, который парсит JSON-слепок и выполняет `UPSERT` для восстановления состояния каталога арендатора.
* **Защита финансовых данных (Immutability):** Реализовать логику так, чтобы при откате цен (`default_price_rub`) в таблицах `positions` и `subcategory_positions`, исторические данные в `order_items` строго игнорировались и не перезаписывались.
* **Защита партионного учета (Inventory Guard):** При итерации по JSON-бэкапу для восстановления товарных единиц (`inventory_items`) внедрить проверку текущего состояния в БД:
* Если единица имеет статус `AVAILABLE` или удалена (`is_deleted = True`) ➔ безопасно восстанавливать (`UPSERT`).
* Если единица уже продана (статус `ISSUED` или существует связь в `order_item_inventory`) ➔ **пропускать (skip)** эту запись. В финальный отчет о восстановлении добавлять предупреждение: *"Товар [UUID] не восстановлен, так как был продан после создания бэкапа"*.


* **Разрешение конфликтов связей:** При восстановлении сущностей из состояния мягкого удаления обновлять флаг `is_deleted = False`, сохраняя консистентность Foreign Keys для уже существующих транзакций.
