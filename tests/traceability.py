"""Registry of milestone I requirements and acceptance scenarios (traceability matrix).

Sources: ``.tasks/tg-shop-platform-tasks/PLAN.md`` sections 3.1 (R1-R8) / 3.3 (S1-S6) / 6A and
the "Этап I" criteria of ``docs/delivery-milestones.md``. Tests reference these codes with
``@pytest.mark.req("R2", ...)``; ``tests/unit/test_traceability.py`` keeps markers and the
registry in sync, ``ops/scripts/test_report.py`` renders the customer report from both.

Only the standard library is used here: the report script imports this module directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["auto", "manual", "deferred"]

#: ``auto`` - must be covered by at least one test; ``manual`` - checked by hand during the demo;
#: ``deferred`` - outside milestone I (``scope_note`` says when).
STATUSES: frozenset[str] = frozenset({"auto", "manual", "deferred"})


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    title: str
    customer_text: str
    scope_note: str = ""
    status: Status = "auto"


_ITEMS: tuple[Requirement, ...] = (
    Requirement(
        "R1",
        "Фундамент разворачивается одной командой",
        "Серверная инфраструктура в Docker разворачивается одной командой; миграции базы данных "
        "применяются автоматически при каждом развёртывании.",
        "Автотесты: миграции на пустой БД (migrate.sh, alembic head, модели = миграции) и "
        "готовность сервисов. Развёртывание на чистом хосте — ручная проверка (MAN-HOST).",
    ),
    Requirement(
        "R2",
        "Разделение данных между магазинами (tenant-изоляция)",
        "Данные магазинов разделены: операции с данными одного магазина не видят и не меняют "
        "данные другого.",
    ),
    Requirement(
        "R3",
        "Централизованные права доступа (RBAC)",
        "Ядро прав доступа: роли, персональные права, разделение по магазинам, быстрая проверка "
        "прав через кэш, отчёт «фактические права пользователя» с источником права.",
        "Экран «фактические права» в Telegram — веха II; в вехе I проверен сервис отчёта.",
    ),
    Requirement(
        "R4",
        "Шифрование токенов ботов",
        "Токены ботов хранятся в зашифрованном виде, с возможностью сменить ключ шифрования.",
    ),
    Requirement(
        "R5",
        "Файловое хранилище независимо от реализации",
        "Файловое хранилище MinIO подключено через общий интерфейс: переход на S3 не требует "
        "изменений в программе.",
    ),
    Requirement(
        "R6",
        "Финансовая неизменяемость",
        "Баланс и бухгалтерия: все операции фиксируются в неизменяемом журнале, повторная оплата "
        "одного и того же исключена.",
        "Не входит в веху I: Этап III «Торговый контур».",
        "deferred",
    ),
    Requirement(
        "R7",
        "Логи не ломают работу и не теряются",
        "Журналы действий пользователей и аудит разбиты по месяцам и автоматически очищаются по "
        "истечении срока хранения; аудит архивируется в хранилище.",
        "Рамка вехи I: LogBuffer, файловые логи, партиции заранее, logs_dropped_total и алерты. "
        "Просмотр/фильтры/экспорт TXT — веха II; «сбой логов не валит покупку» — веха III.",
    ),
    Requirement(
        "R8",
        "Наблюдаемость и восстановимость",
        "Проверки работоспособности всех компонентов, сбор логов и метрик, оповещения о сбоях, "
        "проверка воспроизводимости восстановления.",
        "Проверено на одном хосте; алерт PgPartmanBgwDown — тест promtool. Контракт heartbeat "
        "воркеров готов и проверен; публикация heartbeat воркерами и алерт "
        "WorkerHeartbeatMissing — веха II; backup по расписанию — Этап IV; VPS 2 и "
        "Telegram-алерты — DEF-VPS2.",
    ),
    Requirement(
        "S1",
        "Чистый хост: всё healthy, партиции созданы",
        "После развёртывания все сервисы работают, миграции прошли, помесячные разделы журналов "
        "созданы на 3 месяца вперёд.",
        "Автотесты: migrate.sh на пустой БД, part_config и заранее созданные партиции. "
        "Прогон «cp .env.example → make up» на чистом хосте — ручной (MAN-HOST).",
    ),
    Requirement(
        "S2",
        "Граница месяца и DEFAULT-партиция",
        "Событие в последнюю секунду месяца попадает в раздел этого месяца; событие с датой вне "
        "диапазона не теряется.",
    ),
    Requirement(
        "S3",
        "Защита срока хранения аудита",
        "Отключить автоочистку/архивирование аудита в обход правил нельзя: база и скрипт "
        "синхронизации отвергают испорченные настройки и ничего не пишут.",
    ),
    Requirement(
        "S4",
        "Шифрование и смена ключа",
        "В базе токен хранится не открытым текстом; после смены ключа старые значения читаются, "
        "новые пишутся новым ключом.",
    ),
    Requirement(
        "S5",
        "Отказоустойчивость логирования и прав",
        "Сбой записи журнала не ломает работу (потеря учитывается в метрике), а проверка прав "
        "работает и при недоступном кэше Redis.",
        "Суженная рамка вехи I: сбой БД/файла/очереди LogBuffer + fallback PermissionService на "
        "PostgreSQL. Сценарий «покупка при сбое логгера» — веха III.",
    ),
    Requirement(
        "S6",
        "Воспроизводимость восстановления",
        "Пустая база данных поднимается миграциями, тестовый файл читается из хранилища — "
        "процедура восстановления воспроизводима.",
        "Smoke-проверка процедуры (docs/ops/recovery.md); полноценный backup по расписанию — "
        "Этап IV.",
    ),
    Requirement(
        "SEC1",
        "Защищённый доступ к мониторингу",
        "Доступ к дашбордам и журналу ошибок — только по HTTPS и логину/паролю; ошибки "
        "приложения отправляются по внутренней сети.",
        "Автотесты: генерация Basic Auth (make obs-auth), запрет публичного DSN, конфигурация "
        "CrowdSec/catch-all (ключ bouncer, whitelist, Caddyfile). Вход в UI через Caddy и бан "
        "сканеров — ручная проверка на демо.",
    ),
    Requirement(
        "MAN-HOST",
        "Развёртывание на чистом хосте",
        "На чистом сервере платформа поднимается командами make env / make up, все сервисы "
        "здоровы (make ready).",
        "Ручная проверка перед демо и на демо (docs/reports/milestone-I-demo.md, шаг 1).",
        "manual",
    ),
    Requirement(
        "MAN-UI",
        "Дашборды и журнал ошибок",
        "Доступны дашборды Grafana, трассировка запросов (Tempo) и журнал ошибок urgentry.",
        "Ручная проверка UI на демо (шаги 3–4): дашборды platform_overview/datastores/logs, "
        "трейс make otel-smoke в Tempo/RED, логи по trace_id и событие urgentry с тем же trace_id. "
        "Конфигурация покрыта автотестами: tests/unit/test_telemetry.py, test_observability.py, "
        "test_obs_bootstrap_script.py, test_obs_crowdsec.py, test_otel_smoke_script.py, "
        "promtool-правило TempoDown.",
        "manual",
    ),
    Requirement(
        "DEF-VPS2",
        "Мониторинг на отдельном сервере",
        "Мониторинг на отдельном сервере, оповещения о сбоях в Telegram.",
        "Стек готов к отдельному серверу и проверен на одном хосте; развёртывание на VPS 2 через "
        "VPN и доставка алертов в Telegram — при появлении сервера (PLAN.md 6A).",
        "deferred",
    ),
    Requirement(
        "DEF-BACKUP",
        "Резервное копирование по расписанию",
        "Автоматическое резервное копирование базы данных и файлов, проверенная процедура "
        "восстановления.",
        "Этап IV; в вехе I — только проверка воспроизводимости восстановления (S6).",
        "deferred",
    ),
)

REQUIREMENTS: dict[str, Requirement] = {item.id: item for item in _ITEMS}

#: Requirements covered by the promtool alert-rule tests (not a pytest source).
PROMTOOL_REQS: tuple[str, ...] = ("R8",)
