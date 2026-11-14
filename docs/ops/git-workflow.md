# Работа с репозиторием (с вехи II)

Правила действуют с вехи II. Веха I велась прямыми коммитами в `development`; её история и
теги (`milestone-1`, `v0.1.1.*`) не меняются.

## Ветки

| Ветка | От чего создаётся | Назначение | Куда вливается |
|---|---|---|---|
| `development` | — | интеграционная ветка; её состояние разворачивается на Staging | — |
| `feature/milestone-{N}` | `development` | вся работа над вехой N | `development` через PR, после закрытия вехи |
| `bugfix/milestone-{N}` | `development` | исправление замечаний заказчика по сданной вехе N | `development` через PR |

- Прямые коммиты в `development` запрещены — только слияние PR в интерфейсе GitHub.
- После слияния PR ветка `feature/milestone-{N}` / `bugfix/milestone-{N}` удаляется
  (локально и на remotes).
- Новая ветка всегда создаётся от актуального `development`:

  ```bash
  git fetch github
  git switch -c feature/milestone-N github/development
  ```

## Цикл сдачи вехи

```
feature/milestone-N ──PR──▶ development ──CI/CD──▶ Staging ──▶ тестирование заказчиком
                                 ▲                                     │
                                 └──PR── bugfix/milestone-N ◀── замечания
```

1. Веха N закрыта в `feature/milestone-{N}` → PR в `development` → слияние → ветка удаляется.
2. Слияние PR в `development` запускает CI/CD, который обновляет Staging.
3. Заказчик тестирует Staging.
4. Найденные ошибки исправляются в `bugfix/milestone-{N}` (от `development`) → PR в
   `development` → Staging обновляется → повторное тестирование.
5. Шаги 3–4 повторяются, пока не устранены все замечания.

Параллельно с устранением замечаний по вехе N идёт работа над вехой N+1 в
`feature/milestone-{N+1}` (создаётся от `development` после слияния PR вехи N).

## CI/CD

Триггер обновления Staging — слияние PR в `development` (push в `development` на GitHub).
Реализуется на GitHub Actions в вехе II (см. [delivery-milestones.md](../delivery-milestones.md),
Этап II). До этого Staging обновляется вручную.

## Теги и версия

Версия считается как `git rev-list --count milestone-{N}..HEAD` + 1
(см. [versioning.md](versioning.md)). Влияние схемы с PR:

- `rev-list --count` считает все коммиты, достижимые из HEAD, включая коммиты влитых веток и
  сам merge-коммит. Слияние PR увеличивает счётчик на число коммитов ветки + 1 (merge-коммит).
  Счётчик на `development` растёт монотонно — схема не ломается.
- Одна и та же версия может оказаться у разных коммитов в разных ветках (например,
  `feature/milestone-{N+1}` и `bugfix/milestone-{N}` от одной точки). Однозначный
  идентификатор сборки — пара `version` + `commit` (оба видны в `/version` и
  `app_build_info`).
- Коммит в ветке и merge-коммит в `development` имеют разные версии: на Staging работает
  версия merge-коммита, а не последнего коммита ветки.
- Режим слияния PR — **Create a merge commit**. При squash/rebase коммиты ветки получают
  новые хеши, и «проверенный commit» из отчёта и тег, поставленный в ветке, не окажутся в
  истории `development`.
- Тег начала вехи `milestone-{N}` ставится на первый коммит `feature/milestone-{N}`, как
  описано в [versioning.md](versioning.md#как-сменить-веху).

Тег сдачи `v{версия}` по-прежнему аннотированный и не переносится. На каком коммите его
ставить в новой схеме — см. открытые вопросы.

## Открытые вопросы

1. **GitHub — основной `origin`.** Сейчас `origin` — gitflic, `github` — GitHub. GitHub
   должен стать основным `origin` (PR и CI/CD — на GitHub); переименование remotes пока не
   выполнено, команды в этом документе используют `github`.
2. **Синхронизация gitflic.** Как обновлять второй репозиторий (gitflic) после слияния PR на
   GitHub (вручную `git push`, зеркалирование, шаг CI) — не решено.
3. **Коммит тега сдачи.** Действующее правило: тег = версия коммита с отчётом
   `docs/reports/milestone-N-tests.md`. Если отчёт коммитится в ветке и вливается через PR,
   версия коммита отчёта не совпадает с версией merge-коммита, который развёрнут на Staging.
   Варианты: (а) тег на merge-коммите PR в `development` (= сборка Staging), отчёт — внутри
   PR; (б) тег на коммите отчёта в ветке, как в вехе I. Решение не принято.
4. **Версия `bugfix/milestone-{N}` после слияния вехи N+1.** Когда в `development` влита
   `feature/milestone-{N+1}` (`pyproject.toml` = `0.1.{N+1}`, тег `milestone-{N+1}`),
   исправления вехи N, сделанные от `development`, получат версию `0.1.{N+1}.*`. Нужна ли
   отдельная нумерация для bugfix вехи N — не решено.
