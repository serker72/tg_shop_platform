# Веха I — исправление замечаний (повторная сдача)

- **Веха:** I, повторная сдача после замечаний заказчика по тестированию стенда.
- **Ветка:** `bugfix/milestone-1` (от `development`, базовый тег `v0.1.1.69`, `development` = `87688a8`).
- **Тег повторной сдачи и проверенный коммит** — в таблице тегов [docs/ops/versioning.md](../ops/versioning.md)
  и в отчёте автотестов [milestone-I-tests.md](milestone-I-tests.md) (поле **Git**).

## Содержание

1. [Замечания заказчика](#замечания-заказчика)
   1. [README из вехи II, адаптированный под веху I](#1-readme-из-вехи-ii-адаптированный-под-веху-i)
   2. [`make agent-up`: коды выхода 124 и 2](#2-make-agent-up-коды-выхода-124-и-2)
   3. [`make check`: код выхода 127 без uv](#3-make-check-код-выхода-127-без-uv)
2. [Проверка на стенде](#проверка-на-стенде)
3. [Сопутствующие исправления](#сопутствующие-исправления)
4. [Изменения для пользователя](#изменения-для-пользователя)

## Замечания заказчика

| № | Замечание | Статус |
|---|---|---|
| 1 | Перенести README из вехи II (актуальная структура развёртывания) | ✅ исправлено |
| 2 | `make agent-up` завершается с кодами 124 и 2 | ✅ исправлено |
| 3 | `make check` завершается с кодом 127 на сервере без uv | ✅ исправлено |

### 1. README из вехи II, адаптированный под веху I

**Замечание.** README вехи I устарел относительно структуры развёртывания, описанной в вехе II.

**Исправление.** `README.md` взят из ветки `feature/milestone-2` и адаптирован под веху I:

- убрано то, чего в вехе I нет: `make superuser`, `make env-role` (`ops/scripts/env_role.sh`),
  раздел «Master Bot», ссылки на документы вехи II;
- сценарий «VPS 1 + VPS 2» переписан под веху I: у каждого хоста свой `.env`, с VPS 2 на VPS 1
  переносится только значение `SENTRY_DSN`;
- описание `make agent-up` сверено с Makefile: при `ALLOY_SINGLE_HOST=1` нужна сеть
  мониторинга (сначала `make obs-up`), после запуска вызывается `make ready`;
- добавлены раздел «Тесты» (см. [Изменения для пользователя](#изменения-для-пользователя)),
  требование «uv нужен только для разработки» и назначение `python3` на хосте.

Все упомянутые в README цели `make` и относительные ссылки существуют в ветке.

**Коммиты:** `1583000` docs: README from feature/milestone-2 adapted to milestone I;
`9b320d8` docs: README python3 host requirement.

### 2. `make agent-up`: коды выхода 124 и 2

**Замечание.** `make agent-up` в интерактивном терминале завершается ошибками с кодами 124 и 2.

**Причина.** `make agent-up` после запуска стека вызывает `make ready`, а та запускает
`timeout 120 bash ops/scripts/wait_ready.sh`. Скрипт проверяет `/health/ready` командой
`timeout 15 docker compose exec -T <сервис> python -c ...`.

- `timeout` (без `--foreground`) запускает команду в фоновой группе процессов;
- `docker compose exec -T` не выделяет TTY, но читает stdin; если stdin — терминал, фоновый
  процесс получает `SIGTTIN` и останавливается, а `SIGTERM` вложенного `timeout 15` его не
  завершает;
- через 120 с внешний `timeout` прерывает скрипт и возвращает **124**; `make` сообщает об
  ошибке рецепта `ready` и родительской цели `agent-up` кодом **2**.

Без терминала (CI, фоновый запуск) ошибка не воспроизводится. Это касалось и `make up`,
и отдельного `make ready`.

**Исправление.** В `ops/scripts/wait_ready.sh` проба запускается как
`timeout -k 5 15 docker compose ... exec -T ... </dev/null`: stdin закрыт, `-k 5` добивает
зависший процесс через `SIGKILL`. Других `docker compose exec/run` под `timeout` в Makefile и
скриптах нет. Добавлен регресс-тест (`tests/unit/test_unit_tooling.py`): заглушка `docker`
падает, если stdin пробы не `/dev/null`.

**Коммиты:** `d199ca3` fix(ops): wait_ready probe hangs under timeout on TTY (rc 124/2);
`797bf3e` (регресс-тесты).

**Как проверить** (в интерактивном терминале, на одном хосте с `ALLOY_SINGLE_HOST=1`):

```bash
make obs-up && make agent-up; echo rc=$?
# ожидается: строка "ready: master_bot bot_worker (/health/ready 200)" и rc=0
make ready; echo rc=$?    # rc=0
```

### 3. `make check`: код выхода 127 без uv

**Замечание.** На сервере без uv `make check` завершается с кодом 127 (команда не найдена).

**Причина.** Все зависимости `make check` (`lint`, `format-check`, `test`) вызывали
`uv run ...`, то есть требовали uv и dev-зависимости на хосте.

**Исправление.**

- Новая стадия `test` в `Dockerfile` (dev-зависимости по `pyproject.toml` + `uv.lock`,
  `make`, `git`, `openssl`, `bash`). Production-стадия остаётся последней и собирается по
  умолчанию; production-образ не изменился.
- `make test-unit` — unit-тесты в Docker: код монтируется в контейнер только для чтения,
  процесс работает от текущего пользователя; uv на хосте не нужен.
- `make check` = `make test-unit`; `make test` — алиас `make test-unit`.
- Прежние проверки через uv для разработки: `make test-unit-dev` (unit-тесты на хосте) и
  `make check-dev` (линтеры + проверка форматирования + unit-тесты).

**Коммиты:** `c78ac60` feat(make): test-unit in docker, test-unit-dev via uv, check without uv (rc 127);
`825c52e` fix(make): test-unit runs image by id, read-only mount, --init; test alias to test-unit; clean removes test images.

**Как проверить** (на хосте без uv):

```bash
make check; echo rc=$?    # итог pytest без failed, rc=0
make test; echo rc=$?     # то же
```

## Проверка на стенде

Стенд `/opt/tg_shop_platform` (ветка `bugfix/milestone-1`), интерактивный терминал (через
`script`), uv на хосте отсутствует:

| Команда | Результат |
|---|---|
| `make init-dirs` | rc 0 |
| `make obs-up` | rc 0 |
| `make up` | rc 0 |
| `make agent-up` | rc 0, `ready: master_bot bot_worker` |
| `make ready` | rc 0 |
| `make check` | rc 0, 794 passed, 2 skipped |
| `make test` | rc 0, 794 passed, 2 skipped |

Пропущенные тесты в Docker (на хосте через `make test-unit-dev` они выполняются):

- `test_obs_auth_script.py::test_obs_auth_writes_base64_bcrypt` — в контейнере нет docker CLI
  (тест запускает контейнер; проброс docker-сокета в тестовый контейнер намеренно не делается);
- `test_version.py::test_scripts_are_shellcheck_clean` — в образе нет shellcheck (линт
  shell-скриптов).

## Сопутствующие исправления

- **`unexport UNBAN` в начале Makefile** (`54494cc`). GNU make ≥ 4.4 передаёт экспортируемые
  переменные в окружение `$(shell ...)`, а переменные командной строки раскрываются при
  экспорте: `make obs-bans UNBAN='$(shell cmd)'` выполнял `cmd` при разборе Makefile. Защита
  `unexport UNBAN` стояла после первого `$(shell)` и не успевала сработать. На make 4.3 (хост
  разработки) не воспроизводится; обнаружено новым `make test-unit` (Debian trixie, make 4.4.1).
- **`make test-unit`** (`825c52e`): образ запускается по ID (несколько клонов на одном хосте
  не конкурируют за общий тег), каталог проекта монтируется только для чтения, `--init`;
  `make clean` удаляет тестовые образы по метке `tg_shop_platform.role=unit-test`.
- **Dockerfile** (`d10c5b4`): версии базовых образов вынесены в `ARG PYTHON_IMAGE` /
  `ARG UV_IMAGE`; production-образ идентичен (те же слои и конфигурация).
- **Изоляция `test_encrypted_string` от `.env`** (`2f7888f`): тест задаёт ключ шифрования
  сам и проходит на клоне без `.env`.
- **Регресс-тесты** `tests/unit/test_unit_tooling.py` (`797bf3e`, `f5f8201`): stdin пробы
  `wait_ready.sh`, цели `check`/`test`/`test-unit` без uv, `unexport UNBAN` до первого
  `$(shell)`, production-стадия Dockerfile последняя.
- **Новые цели** `make check-dev` и `make test-unit-dev`.

## Изменения для пользователя

| Цель | Что делает | Нужен uv |
|---|---|---|
| `make check` | unit-тесты в Docker (`make test-unit`) | нет |
| `make test` | алиас `make test-unit` | нет |
| `make test-unit` | unit-тесты в Docker (стадия `test` Dockerfile) | нет |
| `make test-unit-dev` | unit-тесты на хосте (`uv run pytest tests/unit`) | да |
| `make check-dev` | линтеры + проверка форматирования + unit-тесты через uv | да |

Отдельный файл или аргументы pytest:
`make test-unit UNIT_TESTS=tests/unit/test_encrypted_string.py PYTEST_ARGS=-v`.

uv по-прежнему нужен только целям разработки: `make install`, `make lint`, `make format`,
`make format-check`, `make test-unit-dev`, `make check-dev`, `make env-test`,
`make test-integration`, `make test-report`. Для развёртывания uv не нужен.
