# Версионирование приложения

## Схема

`0.1.{веха}.{номер коммита в вехе}` (PEP 440), например `0.1.1.17`.

| Часть | Откуда |
|---|---|
| `0.1.{веха}` | `[project] version` в `pyproject.toml` (хранится только база) |
| номер коммита | `git rev-list --count milestone-{веха}..HEAD` + 1 (коммит с тегом = `.1`) |
| `+dirty` | рабочее дерево с незакоммиченными/неотслеживаемыми (не ignored) изменениями |
| `+unknown` | версию не удалось вычислить (нет git/тега) — только в коде приложения |

Тег начала вехи — аннотированный `milestone-{веха}`. Веха I: `milestone-1` на первом коммите
репозитория.

## Как сменить веху

1. В `pyproject.toml`: `version = "0.1.N"` (новая веха `N`), затем `uv lock` (версия проекта
   записана в `uv.lock`).
2. Тег на первом коммите вехи: `git tag -a milestone-N <sha> -m "Milestone N start"`
   (и `git push origin milestone-N`, когда репозиторий публикуется).

Без тега `ops/scripts/version.sh` (и сборка через `make up/agent-up/migrate/test-integration`)
завершается ошибкой с подсказкой; приложение при этом не падает, а сообщает `0.1.N+unknown`.

### Свежий клон: тег не приходит сам

Локальный тег не попадает в `git clone`, пока его не отправили на удалённый репозиторий
(`git push origin milestone-N`). Клон без тега → `make up` падает с
`ERROR: tag milestone-N not found` и подсказкой. Порядок:

```bash
git fetch --tags                 # подтянуть теги с remote
git tag -l 'milestone-*'         # тег должен появиться
make version                     # 0.1.N.<коммит>
```

Если после `git fetch --tags` тега нет — его не отправили на remote: выполнить
`git push origin milestone-N` в репозитории, где тег создан.

## Тег сдачи вехи

Аннотированный тег `v{версия}`, где версия — вывод `ops/scripts/version.sh` на коммите сдачи
(без `+dirty`). Веха I: `v0.1.1.24` на коммите отчёта `3716134`.

История тегов сдачи вехи I:

| Тег | Коммит | Что |
|---|---|---|
| `v0.1.1.24` | `3716134` | первая сдача (тесты на `b88fecd`) |
| `v0.1.1.27` | `22272d4` | отчёт перегенерирован (тесты на `fedee22`, версия 0.1.1.26; unit 708, integration 159, promtool 15 правил), 2026-11-13 |
| `v0.1.1.41` | `cfc4121` | повторная сдача после доработки (OTel/Tempo/urgentry): тег на коммите нового отчёта |
| `v0.1.1.69` | `9aaa4ec` | повторная сдача (ресурсы хоста, OOM-мониторинг; тесты на `b67e386`, версия 0.1.1.68; unit 787, integration 159, promtool 17 правил), 2026-11-14 |
| `v0.1.1.82` | `1725008` | повторная сдача после замечаний (README, `make agent-up` rc 124/2, `make check` rc 127 без uv — [milestone-I-fixes.md](../reports/milestone-I-fixes.md); ветка `bugfix/milestone-1`, тесты на `90f3b1f`, версия 0.1.1.81; unit 796, integration 159, promtool 17 правил), 2026-11-20 |

- Ставится на коммит с финальным отчётом `docs/reports/milestone-N-tests.md`.
- Сообщение тега: коммит, на котором прогонялись тесты (поле «проверенный commit» в отчёте),
  и итоги прогона.
- `milestone-N` остаётся тегом начала вехи и не переносится. `version.sh` и
  `app/core/version.py` считают версию только от `milestone-N`, поэтому тег сдачи версию
  не меняет.
- Поле **Git** отчёта `make test-report` — `git describe --tags --always`, т.е. ближайший
  тег любого вида: после тега сдачи там будет `v0.1.1.24-<n>-g<sha>`, а не `milestone-1-…`.
  На версию это не влияет.

```bash
git tag -a v<версия> <sha> -m "Milestone N accepted (tests verified on <sha>)

<итоги>"
git push origin v<версия>
```

Существующие теги не переносить и не перезаписывать: без `git tag -f` и `git push --force`.

## Где видна версия

- `make version` / `ops/scripts/version.sh` — версия; `--commit` — короткий хеш;
  `--env` — `APP_VERSION=…`, `APP_COMMIT=…`, `APP_BUILD_TIME=…` (UTC ISO 8601).
- `GET /version` HTTP-сервера health/metrics (порт `OBSERVABILITY_HTTP_PORT`, на хост не
  публикуется): `{"version", "commit", "build_time", "component", "environment"}`.
  Проверка: `docker compose exec master_bot python -c "import urllib.request as u;
  print(u.urlopen('http://127.0.0.1:8080/version').read().decode())"`.
- Prometheus: `app_build_info{version, commit, component} 1` (по одной серии на процесс).
- Sentry SDK → urgentry: `release = tg-shop-platform@<version>`; `SENTRY_RELEASE` в `.env`
  переопределяет значение (пусто — вычисленная версия).

## Порядок определения версии в приложении (`app/core/version.py`)

1. `APP_VERSION`/`APP_COMMIT`/`APP_BUILD_TIME` из окружения (вшиты в образ при сборке);
2. git рабочей копии (если есть `.git` и `git`, с таймаутом) — локальный запуск без Docker;
3. `{база из pyproject.toml}+unknown`.

## Важно: версия фиксируется при сборке образа

`Dockerfile` получает `ARG APP_VERSION/APP_COMMIT/APP_BUILD_TIME` (последний слой, кэш
зависимостей не сбрасывается) и записывает их в `ENV`. Цели `make up`, `agent-up`, `migrate`,
`test-integration` экспортируют значения `ops/scripts/version.sh --env` перед `docker compose`.

- `docker compose up` **без** `--build` (или без экспорта переменных) оставляет старую версию
  образа; прямой вызов `docker compose build` без экспорта даёт `+unknown`.
- Ручная пересборка с версией:

  ```bash
  set -a; eval "$(ops/scripts/version.sh --env)"; set +a
  docker compose --env-file .env up -d --build master_bot bot_worker
  ```
