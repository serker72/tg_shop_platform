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

## Где видна версия

- `make version` / `ops/scripts/version.sh` — версия; `--commit` — короткий хеш;
  `--env` — `APP_VERSION=…`, `APP_COMMIT=…`, `APP_BUILD_TIME=…` (UTC ISO 8601).
- `GET /version` HTTP-сервера health/metrics (порт `OBSERVABILITY_HTTP_PORT`, на хост не
  публикуется): `{"version", "commit", "build_time", "component", "environment"}`.
  Проверка: `docker compose exec master_bot python -c "import urllib.request as u;
  print(u.urlopen('http://127.0.0.1:8080/version').read().decode())"`.
- Prometheus: `app_build_info{version, commit, component} 1` (по одной серии на процесс).
- Sentry/GlitchTip: `release = tg-shop-platform@<version>`; `SENTRY_RELEASE` в `.env`
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
