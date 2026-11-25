from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from aiogram.methods import SendMessage, TelegramMethod
from aiogram_dialog.test_tools.bot_client import FakeBot

from app.modules.logging.filter_options import FilterOption
from app.modules.logging.models import UserActivity
from app.modules.logging.presentation.tg_master.logs import _line, _parse_value
from app.modules.logging.query import ActivityQueryRepository, ActivityRow, ActivityScope, Page
from tests.unit._master_bot_env import ACTOR, SUPER, VIEWER, Env, _user


def test_input_and_bounded_html() -> None:
    assert _parse_value("uid", "123") == 123
    assert _parse_value("username", "@A_B") == "a_b"
    assert _parse_value("start_at", "2026-11-21 00:00Z") == "2026-11-21T00:00:00+00:00"
    for field, value in [("uid", str(2**63)), ("username", "@%"), ("end_at", "2026-11-21")]:
        with pytest.raises(ValueError):
            _parse_value(field, value)
    a = UserActivity(
        id=uuid4(),
        created_at=datetime(2026, 11, 21, 11, 54, tzinfo=UTC),
        telegram_bot_id=1,
        username="name",
        action_type="command",
        button="/start",
    )
    row = ActivityRow(a)
    assert _line(row, 1, "UTC") == "21.11 11:54 | Master | @name | cmd: /start"
    a.button = "<&" * 400
    line = _line(row, None, "UTC")
    assert "<" not in line and "&lt;" in line and str(a.id) not in line
    assert len(line) * 12 < 3000


@pytest.mark.req("R-II.6")
async def test_applied_filters_navigation_and_export(tmp_path: Path) -> None:
    a = UserActivity(
        id=uuid4(),
        created_at=datetime(2026, 11, 21, 11, 54, tzinfo=UTC),
        telegram_bot_id=1,
        username="name",
        action_type="command",
        button="/start",
    )
    marker = f"{a.created_at.isoformat()}|{a.id}"
    rows = [
        UserActivity(
            id=uuid4(),
            created_at=a.created_at,
            telegram_bot_id=1,
            username=f"viewer_{i:02d}",
            action_type="command",
            button="/start",
        )
        for i in range(12)
    ]
    rows.append(a)
    calls = []

    async def search(
        f: Any, order: str, cursor: Any = None, *, scope: Any, backward: bool = False
    ) -> Page:
        calls.append((f, order, cursor, scope, backward))
        ordered = rows if order == "newest" else rows[::-1]
        items = ordered[-1:] if cursor and not backward else ordered[:12]
        return Page(
            [ActivityRow(row) for row in items],
            marker if not cursor or backward else None,
            marker if cursor and not backward else None,
            not cursor or backward,
            13,
        )

    exported = tmp_path / "logs.txt"
    exported.write_text("log", encoding="utf-8")
    repository = AsyncMock(search=search, decode_cursor=ActivityQueryRepository.decode_cursor)
    repository.count.side_effect = AssertionError("render must obtain total from search")
    exporter = AsyncMock()
    exporter.export_txt.return_value = exported
    env = Env(
        SUPER, activity_repository=repository, export_service=exporter, master_telegram_bot_id=1
    )
    await env.start()
    await env.press("Логи")
    assert "1/2" in env.text()
    assert "Порядок по времени события: сначала новые" in env.text()
    assert "Показать сначала старые" in env.buttons()
    assert env.text().splitlines()[-12:] == [
        f"21.11 11:54 | Master | @viewer_{i:02d} | cmd: /start" for i in range(12)
    ]
    assert env.text().count("cmd: /start") == 12
    assert all(str(row.id) not in env.text() for row in rows)
    before = calls[-1][0]
    assert before.end_at - before.start_at == timedelta(hours=24)
    await env.press("→")
    assert "2/2" in env.text() and "@name" in env.text() and "@viewer_00" not in env.text()
    before_back = len(calls)
    await env.press("←")
    assert "1/2" in env.text() and calls[-1][-1]
    assert len(calls) == before_back + 1
    await env.press("→")
    queries_before_draft = len(calls)
    await env.press("Фильтры")
    assert "ID товара" not in env.buttons()
    await env.press("Пользователи")
    await env.press("Telegram ID")
    assert "Пример: 123456789" in env.text()
    await env.send("123")
    await env.press("Назад")
    assert "✓ Пользователи" in env.buttons()
    await env.press("Очистить")
    assert "Пользователи" in env.buttons()
    assert len(calls) == queries_before_draft
    await env.press("Отмена")
    assert calls[-1][0] == before and "2/2" in env.text()
    await env.press("Фильтры")
    await env.press("Пользователи")
    await env.press("Telegram ID")
    await env.send("456")
    await env.press("Назад")
    await env.press("Действие")
    await env.press("Тип действия")
    await env.send("command")
    await env.press("Назад")
    await env.press("Применить")
    assert (calls[-1][0].telegram_user_id, calls[-1][0].action_type) == (456, "command")
    assert calls[-1][2] is None and "1/2" in env.text()
    applied, order, _, scope, _ = calls[-1]
    assert order == "newest"
    await env.press("→")
    assert "2/2" in env.text() and calls[-1][2] is not None
    await env.press("Показать сначала старые")
    assert calls[-1] == (applied, "oldest", None, scope, False)
    assert "Порядок по времени события: сначала старые" in env.text()
    assert "1/2" in env.text() and env.text().splitlines()[-12].endswith("@name | cmd: /start")
    assert "←" not in env.buttons() and "Показать сначала новые" in env.buttons()
    await env.press("→")
    assert "2/2" in env.text() and calls[-1][2] is not None
    await env.press("Показать сначала новые")
    assert calls[-1] == (applied, "newest", None, scope, False)
    assert "Порядок по времени события: сначала новые" in env.text()
    assert "1/2" in env.text() and "@viewer_00" in env.text()
    assert "←" not in env.buttons() and "Показать сначала старые" in env.buttons()
    with patch.object(env.client.bot, "send_document", new=AsyncMock()) as send:
        await env.press("Экспорт TXT")
    send.assert_awaited_once()
    assert exporter.export_txt.await_args.args[1] == calls[-1][0]
    exporter.record_delivery_success.assert_awaited_once()
    assert not exported.exists()


@pytest.mark.req("R-II.6")
@pytest.mark.parametrize("change", ["late_insert", "deleted_row"])
async def test_return_to_first_page_recovers_shifted_window(change: str) -> None:
    moment = datetime(2026, 11, 21, 11, 54, tzinfo=UTC)
    cursor = f"{moment.isoformat()}|{uuid4()}"
    decoded = ActivityQueryRepository.decode_cursor(cursor)

    def row(username: str) -> ActivityRow:
        return ActivityRow(
            UserActivity(
                id=uuid4(),
                created_at=moment,
                telegram_bot_id=1,
                action_type="command",
                username=username,
            )
        )

    before = [row(f"viewer_{i:02d}") for i in range(12)]
    tail = row("tail")
    if change == "late_insert":
        initial_total = 13
        tail_rows = [tail]
        backward_page = Page(before, cursor, cursor, True, 14)
        first_page = [row("late_record"), *before[:11]]
        recovered_user, excluded_user = "late_record", "viewer_11"
    else:
        initial_total = 14
        tail_rows = [tail, row("last")]
        backward_page = Page(before[1:], cursor, None, True, 13)
        first_page = [*before[1:], tail]
        recovered_user, excluded_user = "tail", "viewer_00"
    repo = AsyncMock(decode_cursor=ActivityQueryRepository.decode_cursor)
    repo.search.side_effect = [
        Page(before, cursor, None, True, initial_total),
        Page(tail_rows, None, cursor, False, initial_total),
        backward_page,
        Page(first_page, cursor, None, True, backward_page.total),
    ]
    env = Env(VIEWER, activity_repository=repo, master_telegram_bot_id=1)
    await env.start()
    await env.press("Логи")
    await env.press("→")
    assert "2/2" in env.text()
    await env.press("←")
    assert "1/2" in env.text() and f"@{recovered_user}" in env.text()
    assert f"@{excluded_user}" not in env.text() and "←" not in env.buttons()
    assert env.text().count(" | command") == 12
    initial, _, back, reload = repo.search.await_args_list
    assert back.args == (*initial.args[:2], decoded)
    assert back.kwargs == {"scope": initial.kwargs["scope"], "backward": True}
    assert reload.args == initial.args[:2]
    assert reload.kwargs == {"scope": initial.kwargs["scope"]}


@pytest.mark.req("R-II.6")
async def test_back_empty_page_keeps_back_and_resets_cursor_on_retry() -> None:
    moment = datetime(2026, 11, 21, 11, 54, tzinfo=UTC)
    cursor = f"{moment.isoformat()}|{uuid4()}"
    row = ActivityRow(
        UserActivity(id=uuid4(), created_at=moment, telegram_bot_id=1, username="viewer")
    )
    repo = AsyncMock(decode_cursor=ActivityQueryRepository.decode_cursor)
    repo.search.side_effect = [
        Page([row], cursor, None, True, 13),
        Page([row], None, cursor, False, 13),
        Page([], None, None, False, 0),
        Page([row], None, None, False, 1),
    ]
    env = Env(VIEWER, activity_repository=repo, master_telegram_bot_id=1)
    await env.start()
    await env.press("Логи")
    await env.press("→")
    assert "2/2" in env.text()
    await env.press("←")
    assert repo.search.await_count == 3
    initial, _, back = repo.search.await_args_list
    assert back.args == (*initial.args[:2], ActivityQueryRepository.decode_cursor(cursor))
    assert back.kwargs == {"scope": initial.kwargs["scope"], "backward": True}
    assert "Нет событий" in env.text() and "←" in env.buttons()
    await env.press("←")
    assert repo.search.await_count == 4
    reset = repo.search.await_args_list[-1]
    assert reset.args == (*initial.args[:2], None)
    assert reset.kwargs == {"scope": initial.kwargs["scope"], "backward": False}
    assert "1/1" in env.text() and "@viewer" in env.text()
    assert "←" not in env.buttons()


@pytest.mark.req("R-II.6")
async def test_back_to_small_complete_first_page_does_not_reload() -> None:
    moment = datetime(2026, 11, 21, 11, 54, tzinfo=UTC)
    cursor = f"{moment.isoformat()}|{uuid4()}"
    rows = [
        ActivityRow(
            UserActivity(id=uuid4(), created_at=moment, telegram_bot_id=1, username=f"viewer_{i}")
        )
        for i in range(3)
    ]
    repo = AsyncMock(decode_cursor=ActivityQueryRepository.decode_cursor)
    repo.search.side_effect = [
        Page(rows, cursor, None, True, 13),
        Page(rows[:1], None, cursor, False, 13),
        Page(rows, None, None, False, len(rows)),
    ]
    env = Env(VIEWER, activity_repository=repo, master_telegram_bot_id=1)
    await env.start()
    await env.press("Логи")
    await env.press("→")
    assert "2/2" in env.text()
    await env.press("←")
    assert repo.search.await_count == 3
    initial, _, back = repo.search.await_args_list
    assert back.args == (*initial.args[:2], ActivityQueryRepository.decode_cursor(cursor))
    assert back.kwargs == {"scope": initial.kwargs["scope"], "backward": True}
    assert "1/1" in env.text() and "←" not in env.buttons()
    assert all(f"@viewer_{i}" in env.text() for i in range(3))


@pytest.mark.req("R-II.6")
async def test_scoped_picker_rechecks_forged_id_and_bot_shortcut() -> None:
    shop = uuid4()
    other = uuid4()
    expected_scope = ActivityScope(
        frozenset({shop, other}), master_telegram_bot_id=777, actor_telegram_id=ACTOR
    )
    bot = FilterOption("555", "Shop Bot", shop)
    customer = FilterOption("222", "@visitor", shop)
    staff = FilterOption(str(uuid4()), "@staff", None)
    shop_option = FilterOption(str(shop), "Shop A", shop)
    other_bot = FilterOption("556", "Other Bot", other)
    repo = AsyncMock()
    repo.search.return_value = Page([], None, None, False, 0)
    options = AsyncMock()

    def page(kind: str, scope: Any, tenant: Any, offset: int) -> tuple[list[FilterOption], bool]:
        assert scope == expected_scope
        if kind == "visitor":
            assert tenant == shop
        return {"bot": [bot, other_bot], "visitor": [customer], "staff": [staff]}[kind], False

    def get(kind: str, scope: Any, tenant: Any, value: str) -> FilterOption | None:
        assert scope == expected_scope
        if kind == "visitor":
            assert tenant == shop
        return {
            ("bot", "555"): bot,
            ("bot", "556"): other_bot,
            ("visitor", "222"): customer,
            ("staff", staff.key): staff,
            ("shop", shop_option.key): shop_option,
        }.get((kind, value))

    options.page.side_effect = page
    options.get.side_effect = get
    env = Env(
        replace(VIEWER, tenants=frozenset({shop, other})),
        activity_repository=repo,
        filter_options=options,
        master_telegram_bot_id=777,
    )
    await env.start()
    await env.press("Логи")
    assert "0/0" in env.text()
    await env.press("Фильтры")
    await env.press("Боты")
    await env.press("Бот магазина")
    await env.forge("pick:555", "pick:555", "pick:666")
    assert options.get.await_args.args[-1] == "666"
    await env.press("Shop Bot")
    await env.press("Назад")
    await env.press("Пользователи")
    await env.press("Посетители выбранного магазина")
    await env.press("@visitor")
    await env.press("Сотрудник")
    await env.press("@staff")
    await env.press("Telegram ID")
    await env.send("333")
    await env.press("Username")
    await env.send("@snapshot")
    await env.press("Назад")
    await env.press("✓ Боты")
    await env.press("Бот магазина")
    await env.press("Other Bot")
    assert "Магазин посетителя: Shop A" in env.text()
    assert "пересечение пусто" in env.text()
    await env.press("Назад")
    await env.press("Применить")
    applied = repo.search.await_args.args[0]
    assert (applied.bot_id, applied.telegram_user_id, applied.visitor) == (556, 333, (shop, 222))
    assert applied.user_id is not None and str(applied.user_id) == staff.key
    assert applied.username == "snapshot" and applied.tenant_ids == frozenset({other})
    assert "пересечение пусто" in env.text()
    assert repo.search.await_args.kwargs["scope"] == expected_scope
    await env.press("Фильтры")
    await env.press("Очистить")
    await env.press("Боты")
    await env.press("Master Bot")
    await env.press("Назад")
    await env.press("Применить")
    assert repo.search.await_args.args[0].bot_id == 777
    assert repo.search.await_args.kwargs["scope"] == expected_scope


@pytest.mark.req("R-II.6")
@pytest.mark.parametrize("button", ["Показать сначала старые", "Экспорт TXT"])
async def test_open_logs_dialog_rechecks_revoked_permission(button: str) -> None:
    repo = AsyncMock()
    repo.search.return_value = Page([], None, None, False, 0)
    exporter = AsyncMock()
    env = Env(VIEWER, activity_repository=repo, export_service=exporter)
    await env.start()
    await env.press("Логи")
    repo.search.assert_awaited_once()
    assert button in env.buttons()

    env.snapshot = replace(VIEWER, role_permissions={})
    repo.reset_mock()
    with pytest.raises(PermissionError, match=r"logs\.view"):
        await env.press(button)
    repo.search.assert_not_awaited()
    exporter.export_txt.assert_not_awaited()


@pytest.mark.req("R-II.6")
async def test_period_presets_custom_and_visible_input() -> None:
    repo = AsyncMock()
    repo.search.return_value = Page([], None, None, False, 0)
    env = Env(SUPER, activity_repository=repo)
    await env.start()
    await env.press("Логи")
    for label, days in [("7 д", 7), ("30 д", 30), ("24 ч", 1)]:
        await env.press("Фильтры")
        await env.press("✓ Период")
        await env.press(label)
        await env.press("Назад")
        assert "✓ Период" in env.buttons()
        await env.press("Применить")
        applied = repo.search.await_args.args[0]
        assert applied.end_at - applied.start_at == timedelta(days=days)
        assert f"Период: {label}" in env.text()
    await env.press("Фильтры")
    await env.press("Пользователи")
    await env.press("Username")
    assert "Текущее: —" in env.text() and "Пример: @example" in env.text()
    await env.send("@Example_User")
    await env.press("Username")
    assert "Текущее: example_user" in env.text()
    await env.press("Отмена")
    await env.press("Назад")
    await env.press("✓ Период")
    await env.press("30 д")
    await env.press("Начало периода")
    displayed = datetime.fromisoformat(env.text().split("Текущее: ")[1].splitlines()[0])
    assert abs(displayed - (datetime.now(UTC) - timedelta(days=30))) < timedelta(minutes=1)
    assert "Пример:" in env.text()
    await env.press("Отмена")
    assert "Период: 30 д" in env.text()
    await env.press("Конец периода")
    before_end = datetime.fromisoformat(env.text().split("Текущее: ")[1].splitlines()[0])
    await env.send((before_end - timedelta(days=1)).isoformat())
    await env.press("Начало периода")
    kept = datetime.fromisoformat(env.text().split("Текущее: ")[1].splitlines()[0])
    assert abs(kept - (before_end - timedelta(days=30))) < timedelta(seconds=1)
    await env.send("2026-01-01 03:00+03:00")
    await env.press("Конец периода")
    assert "Текущее:" in env.text() and "Пример:" in env.text()
    await env.send("2026-01-02 00:00Z")
    await env.press("Назад")
    assert "✓ Период" in env.buttons()
    await env.press("Применить")
    applied = repo.search.await_args.args[0]
    assert applied.start_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert applied.end_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert applied.username == "example_user"
    await env.press("Фильтры")
    await env.press("✓ Период")
    await env.press("Очистить")
    assert "Период: 24 ч" in env.text()
    await env.press("Назад")
    assert "✓ Период" in env.buttons() and "✓ Пользователи" in env.buttons()
    await env.press("Очистить")
    assert "✓ Период" in env.buttons() and "Пользователи" in env.buttons()
    await env.press("Применить")
    cleared = repo.search.await_args.args[0]
    assert cleared.end_at - cleared.start_at == timedelta(hours=24)
    assert "Период: 24 ч" in env.text()


@pytest.mark.req("R-II.17", "R-II.6")
@pytest.mark.parametrize(
    ("staff_zone", "effective_zone", "displayed_time", "start_hour"),
    [
        ("Europe/Berlin", "Europe/Berlin", "01.02 00:30", 23),
        (None, "Asia/Tokyo", "01.02 08:30", 15),
    ],
    ids=["staff-overrides-default", "non-utc-default"],
)
async def test_local_range_display_and_applied_export(
    tmp_path: Path,
    staff_zone: str | None,
    effective_zone: str,
    displayed_time: str,
    start_hour: int,
) -> None:
    actor, event_user = _user(ACTOR), _user(ACTOR + 1)
    actor.timezone, event_user.timezone = staff_zone, "America/New_York"
    created_at = datetime(2026, 1, 31, 23, 30, tzinfo=UTC)
    activity = UserActivity(
        id=uuid4(),
        created_at=created_at,
        telegram_bot_id=1,
        telegram_user_id=event_user.telegram_user_id,
        username="visitor",
        action_type="click",
    )
    repo = AsyncMock()
    repo.search.return_value = Page([ActivityRow(activity)], None, None, False, 1)
    exporter = AsyncMock()
    exported = tmp_path / "logs.txt"
    exported.write_text("log", encoding="utf-8")
    exporter.export_txt.return_value = exported
    env = Env(
        SUPER,
        users=[actor, event_user],
        default_timezone="Asia/Tokyo",
        activity_repository=repo,
        export_service=exporter,
        master_telegram_bot_id=1,
    )
    await env.start()
    if staff_zone is None:
        await env.press("Пропустить")
    await env.press("Логи")
    zone_label = f"Часовой пояс: {effective_zone}"
    assert zone_label in env.text()
    assert f"{displayed_time} | Master | @visitor | click" in env.text()
    assert activity.created_at is created_at

    await env.press("Фильтры")
    await env.press("✓ Период")
    await env.press("Начало периода")
    assert zone_label in env.text()
    await env.send("2026-02-01 00:00")
    await env.press("Конец периода")
    await env.send("2026-02-01 10:00")
    await env.press("Начало периода")
    offset = "+01:00" if staff_zone else "+09:00"
    assert f"Текущее: 2026-02-01 00:00:00{offset}" in env.text()
    await env.press("Отмена")
    await env.press("Назад")
    repo.search.assert_awaited_once()
    await env.press("Применить")
    applied, order = repo.search.await_args.args[:2]
    expected_start = datetime(2026, 1, 31, start_hour, tzinfo=UTC)
    assert (applied.start_at, applied.end_at) == (
        expected_start,
        expected_start + timedelta(hours=10),
    )
    assert applied.start_at.tzinfo is UTC and applied.end_at.tzinfo is UTC
    assert f"Период: 2026-02-01 00:00:00{offset} – 2026-02-01 10:00:00{offset}" in env.text()
    scope = repo.search.await_args.kwargs["scope"]

    # A valid but cancelled draft must not leak into the list or TXT export.
    await env.press("Фильтры")
    await env.press("✓ Период")
    await env.press("Конец периода")
    await env.send("2026-02-01 11:00")
    await env.press("Назад")
    await env.press("Отмена")
    assert repo.search.await_args.args[:2] == (applied, order)
    with patch.object(env.client.bot, "send_document", new=AsyncMock()) as send:
        await env.press("Экспорт TXT")
    send.assert_awaited_once()
    exporter.export_txt.assert_awaited_once_with(
        ACTOR, applied, order, scope=scope, timezone=effective_zone
    )


@pytest.mark.req("R-II.17", "R-II.6")
@pytest.mark.parametrize(
    ("invalid_local", "hint"),
    [
        ("2026-10-25 02:30", "Укажите числовое смещение UTC"),
        ("2026-03-29 02:30", "Выберите корректное время"),
    ],
    ids=["fold", "gap"],
)
async def test_invalid_dst_input_preserves_draft_and_accepts_explicit_instants(
    invalid_local: str, hint: str
) -> None:
    replies: list[str] = []

    class ReplyBot(FakeBot):
        async def __call__(
            self, method: TelegramMethod[Any], request_timeout: int | None = None
        ) -> Any:
            if isinstance(method, SendMessage):
                replies.append(method.text)
                return
            return await super().__call__(method, request_timeout)

    staff = _user(ACTOR)
    staff.timezone = "Europe/Berlin"
    repo = AsyncMock()
    repo.search.return_value = Page([], None, None, False, 0)
    env = Env(SUPER, users=[staff], activity_repository=repo)
    env.client.bot = ReplyBot()  # type: ignore[no-untyped-call]
    await env.start()
    await env.press("Логи")
    await env.press("Фильтры")
    await env.press("✓ Период")
    await env.press("Начало периода")
    await env.send("2026-10-25 02:30+02:00")
    await env.press("Конец периода")
    await env.send("2026-10-25 01:30Z")
    draft_summary = "2026-10-25 02:30:00+02:00 – 2026-10-25 02:30:00+01:00"
    assert draft_summary in env.text()

    await env.press("Начало периода")
    await env.client.send(invalid_local)
    assert len(replies) == 1 and "Europe/Berlin" in replies[0] and hint in replies[0]
    # The editor must stay open; cancelling the input must reveal the same draft.
    await env.press("Отмена")
    assert draft_summary in env.text()
    repo.search.assert_awaited_once()
    await env.press("Назад")
    await env.press("Применить")
    applied = repo.search.await_args.args[0]
    assert applied.start_at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert applied.end_at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


@pytest.mark.req("R-II.17", "R-II.6")
@pytest.mark.parametrize(
    ("zone", "start_iso"),
    [("Asia/Tokyo", "9999-12-31T16:00:00Z"), ("Etc/GMT+5", "0001-01-01T00:00:00Z")],
    ids=["upper-overflow", "lower-overflow"],
)
async def test_extreme_utc_start_bound_is_applied_and_displayed(zone: str, start_iso: str) -> None:
    start = datetime.fromisoformat(start_iso)
    end = start + timedelta(hours=6)
    utc_label = start_iso.replace("T", " ").removesuffix("Z") + "+00:00 UTC"
    staff = _user(ACTOR)
    staff.timezone = zone
    event = UserActivity(
        id=uuid4(), created_at=start, telegram_bot_id=1, username="visitor", action_type="click"
    )
    repo = AsyncMock()
    repo.search.side_effect = [
        Page([], None, None, False, 0),
        Page([ActivityRow(event)], None, None, False, 1),
    ]
    env = Env(SUPER, users=[staff], activity_repository=repo, master_telegram_bot_id=1)
    await env.start()
    await env.press("Логи")
    await env.press("Фильтры")
    await env.press("✓ Период")
    await env.press("Начало периода")
    await env.send(start_iso)
    await env.press("Конец периода")
    await env.send(end.isoformat())
    await env.press("Начало периода")
    assert f"Текущее: {utc_label}" in env.text()
    await env.press("Отмена")
    await env.press("Назад")
    await env.press("Применить")

    assert repo.search.await_count == 2
    applied = repo.search.await_args.args[0]
    assert (applied.start_at, applied.end_at) == (start, end)
    assert applied.start_at.tzinfo is UTC and applied.end_at.tzinfo is UTC
    assert f"Часовой пояс: {zone}" in env.text()
    assert f"Период: {utc_label} – " in env.text()
    assert f"{utc_label} | Master | @visitor | click" in env.text()
