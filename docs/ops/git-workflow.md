# Работа с репозиторием (с вехи II)

Правила действуют с вехи II. Веха I велась прямыми коммитами в `development`; её история и
теги (`milestone-1`, `v0.1.1.*`) не меняются.

## Ветки

| Ветка | От чего создаётся | Назначение | Куда вливается |
|---|---|---|---|
| `master` | `development` | production; её состояние разворачивается на Production (VPS1, VPS2) | — |
| `development` | — | интеграционная ветка; её состояние разворачивается на Staging | `master` через PR, после принятия вехи заказчиком |
| `feature/milestone-{N}` | `development` | вся работа над вехой N | `development` через PR, после закрытия вехи |
| `bugfix/milestone-{N}` | `development` | исправление замечаний заказчика по сданной вехе N | `development` через PR |

- Прямые коммиты в `development` и `master` запрещены — только слияние PR в интерфейсе GitHub.
- В `master` вливается только `development` через PR, после того как заказчик принял веху.
  Режим слияния — **Create a merge commit**, как и для PR в `development`.
- После слияния PR ветка `feature/milestone-{N}` / `bugfix/milestone-{N}` удаляется
  (локально и на remotes).
- Новая ветка всегда создаётся от актуального `development`:

  ```bash
  git fetch origin
  git switch -c feature/milestone-N origin/development
  ```

## Remotes

| Remote | Репозиторий | Назначение |
|---|---|---|
| `origin` | GitHub (`github.com`) | основной: PR, CI/CD |
| `gitflic` | gitflic (`gitflic.ru`) | зеркало |

Remotes переименованы 2026-11-14 (раньше `origin` = gitflic, `github` = GitHub). Ветки и
теги отправляются в оба: `git push origin <ref>` и `git push gitflic <ref>`.

## Цикл сдачи вехи

```
feature/milestone-N ──PR──▶ development ──CI/CD──▶ Staging ──▶ тестирование заказчиком
                                 ▲                                     │
                                 └──PR── bugfix/milestone-N ◀── замечания

заказчик принял веху N ──▶ development ──PR──▶ master ──CI/CD──▶ Production (VPS1, VPS2)*
```

\* Первая выкладка в Production допускается только в финале [Вехи IV](../delivery-milestones.md#этап-iv-расширенные-модули-и-готовность-к-промышленной-эксплуатации), после успешного переноса и сверки данных старого бота на финальной схеме.

1. Веха N закрыта в `feature/milestone-{N}` → PR в `development` → слияние → ветка удаляется.
2. Слияние PR в `development` запускает CI/CD, который обновляет Staging.
3. Заказчик тестирует Staging.
4. Найденные ошибки исправляются в `bugfix/milestone-{N}` (от `development`) → PR в
   `development` → Staging обновляется → повторное тестирование.
5. Шаги 3–4 повторяются, пока не устранены все замечания.
6. Заказчик принял веху N → PR `development` → `master` → слияние остаётся триггером CI/CD для
   Production (VPS1, VPS2). Для первой выкладки действует условие под схемой.

Параллельно с устранением замечаний по вехе N идёт работа над вехой N+1 в
`feature/milestone-{N+1}` (создаётся от `development` после слияния PR вехи N).

Перенос исправлений в веху N+1 (решение 2026-11-20): сразу после проверки `bugfix/milestone-{N}`
(отчёт, тег сдачи) ветка вливается в `feature/milestone-{N+1}` командой
`git merge --no-ff bugfix/milestone-{N}` (не cherry-pick/rebase: хеши проверенного коммита и
коммита с тегом сохраняются). Конфликты решаются по смыслу; `docs/reports/milestone-{N}-*` —
версии bugfix. Тестовая БД общая для веток: после прогона в одной ветке интеграционные тесты
в другой требуют `make test-integration-down PURGE=1`, иначе alembic не найдёт ревизию.

## CI/CD

Два триггера:

| Событие | Окружение | Когда реализуется |
|---|---|---|
| слияние PR в `development` (push в `development` на GitHub) | Staging | веха II, GitHub Actions (см. [delivery-milestones.md](../delivery-milestones.md), Этап II); до этого Staging обновляется вручную |
| слияние PR в `master` (push в `master` на GitHub) | Production (VPS1, VPS2) | когда появятся серверы Production; до этого — только описание. Для первой выкладки действует условие [под схемой](#цикл-сдачи-вехи) |

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

Тег сдачи `v{версия}` по-прежнему аннотированный и не переносится. Ставится на коммит с
отчётом `docs/reports/milestone-N-tests.md` в ветке (`feature/milestone-{N}` или
`bugfix/milestone-{N}`) до PR — см. открытый вопрос 3 (решён).

`master` и теги:

- Тег сдачи вехи попадает в историю `development` вместе с PR ветки (вопрос 3, вариант б) —
  до вливания в `master`, т.к. в `master` попадает только принятая заказчиком веха.
- PR `development` → `master` в режиме **Create a merge commit** создаёт в `master` новый
  merge-коммит. Его версия отличается от версии коммита с тегом сдачи и от версии, которая
  работала на Staging; коммит с тегом сдачи достижим из `master`.
- На Production работает версия merge-коммита в `master`; однозначный идентификатор —
  пара `version` + `commit`.
- Ставить ли отдельный тег релиза на merge-коммит в `master` — см. открытые вопросы.

## Открытые вопросы

1. ~~**GitHub — основной `origin`.**~~ Решено 2026-11-14: remotes переименованы, `origin` —
   GitHub (PR и CI/CD), `gitflic` — прежний `origin` (см. раздел «Remotes»).
2. **Синхронизация gitflic.** Как обновлять второй репозиторий (gitflic) после слияния PR на
   GitHub (вручную `git push`, зеркалирование, шаг CI) — не решено.
3. ~~**Коммит тега сдачи.**~~ Решено 2026-11-20: вариант (б) — тег сдачи (в том числе
   повторной) ставится на коммит отчёта `docs/reports/milestone-N-tests.md` в
   `bugfix/milestone-{N}` (или `feature/milestone-{N}`) до PR; версия коммита отчёта не
   совпадает с версией merge-коммита на Staging, однозначный идентификатор сборки — пара
   `version` + `commit`. Первое применение: `v0.1.1.82` (повторная сдача вехи I, ветка
   `bugfix/milestone-1`). Отвергнут вариант (а) — тег на merge-коммите PR в `development`.
4. **Версия `bugfix/milestone-{N}` после слияния вехи N+1.** Когда в `development` влита
   `feature/milestone-{N+1}` (`pyproject.toml` = `0.1.{N+1}`, тег `milestone-{N+1}`),
   исправления вехи N, сделанные от `development`, получат версию `0.1.{N+1}.*`. Нужна ли
   отдельная нумерация для bugfix вехи N — не решено.
5. **Тег релиза в `master`.** Ставить ли отдельный тег на merge-коммит PR `development` →
   `master` (сборка Production), его формат и соотношение с тегом сдачи `v{версия}` — не
   решено.
6. **Защита веток `master`/`development`** (branch protection rules в GitHub) — настроить при
   детализации CI/CD вехи II.
