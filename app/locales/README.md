# Каталоги gettext

Исходный язык msgid — русский (`app/core/i18n.py`): пустой `msgstr` в `ru` возвращает сам msgid.

- `make i18n-extract` — собрать строки `_()` из `app/` в `app/locales/messages.pot`;
- `make i18n-update` — обновить `app/locales/<lang>/LC_MESSAGES/messages.po`;
- `make i18n-compile` — скомпилировать `messages.mo`.

`.mo` коммитится вместе с `.po`: образ собирается без dev-зависимостей (Babel), а
`tests/unit/test_i18n.py` проверяет, что каталог `ru` содержит все строки кода.
Новый язык: `uv run pybabel init -i app/locales/messages.pot -d app/locales -D messages -l <lang>`.
