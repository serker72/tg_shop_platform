"""Integration coverage for the bounded activity query and PostgreSQL plan."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot_manager.models import Customer
from app.modules.iam.models import User
from app.modules.logging.export import export_txt
from app.modules.logging.filter_options import ActivityFilterOptions
from app.modules.logging.models import UserActivity
from app.modules.logging.query import ActivityFilter, ActivityQueryRepository, ActivityScope
from tests.integration.test_iam_models import _tenant

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.6")]


def test_activity_filter_has_pruning_range() -> None:
    f = ActivityFilter()
    assert f.start_at.tzinfo is UTC
    assert f.end_at > f.start_at


def test_activity_filter_declares_all_47_6_fields() -> None:
    names = ActivityFilter.__dataclass_fields__
    required = {
        "user_id",
        "telegram_user_id",
        "username",
        "bot_id",
        "tenant_id",
        "start_at",
        "end_at",
        "action_type",
        "entity_type",
        "entity_id",
        "product_id",
        "category_id",
        "subcategory_id",
        "order_id",
        "case_id",
        "preorder_id",
        "moderator",
    }
    assert required <= set(names)


def test_activity_filter_accepts_partition_boundary_and_scope() -> None:
    tenant = uuid4()
    start = datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)
    end = datetime(2026, 2, 1, 0, 0, 1, tzinfo=UTC)
    value = ActivityFilter(
        tenant_ids=frozenset({tenant}),
        master_telegram_bot_id=999,
        start_at=start,
        end_at=end,
        telegram_user_id=123,
        username="alice",
        bot_id=999,
        action_type="OPEN_PRODUCT",
        entity_type="product",
        entity_id=uuid4(),
        product_id=uuid4(),
        category_id=uuid4(),
        subcategory_id=uuid4(),
        order_id=uuid4(),
        case_id=uuid4(),
        preorder_id=uuid4(),
        moderator=123,
    )
    assert value.start_at < value.end_at
    assert value.tenant_ids == frozenset({tenant})


def test_activity_query_explicit_tenant_keeps_scoped_predicate() -> None:
    allowed = uuid4()
    denied = uuid4()
    statement = ActivityQueryRepository._query(
        object.__new__(ActivityQueryRepository),
        ActivityFilter(
            tenant_id=denied,
            tenant_ids=frozenset({allowed}),
            master_telegram_bot_id=999,
            start_at=datetime(2026, 1, 1, tzinfo=UTC),
            end_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        "newest",
        None,
        20,
        ActivityScope(frozenset({allowed}), master_telegram_bot_id=999),
    )
    rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id" in rendered
    assert denied.hex in rendered
    assert allowed.hex in rendered


async def test_activity_username_uses_snapshot_without_users_row(session: AsyncSession) -> None:
    created_at = datetime(2026, 1, 15, 12, tzinfo=UTC)
    activity = UserActivity(
        created_at=created_at,
        telegram_bot_id=7001,
        telegram_user_id=701,
        username="historical_customer",
        action_type="OPEN_PRODUCT",
    )
    session.add(activity)
    await session.flush()

    page = await ActivityQueryRepository(session).search(
        ActivityFilter(
            username="@HISTORICAL_CUSTOMER",
            start_at=datetime(2026, 1, 1, tzinfo=UTC),
            end_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        scope=ActivityScope(frozenset(), all_tenants=True),
    )

    assert [row.activity.id for row in page.items] == [activity.id]


async def test_activity_username_uses_historical_value_after_user_rename(
    session: AsyncSession,
) -> None:
    created_at = datetime(2026, 1, 16, 12, tzinfo=UTC)
    user = User(telegram_user_id=702, telegram_user_name="current_name")
    activity = UserActivity(
        created_at=created_at,
        telegram_bot_id=7002,
        telegram_user_id=702,
        username="previous_name",
        action_type="OPEN_PRODUCT",
    )
    session.add_all([user, activity])
    await session.flush()

    page = await ActivityQueryRepository(session).search(
        ActivityFilter(
            username="previous_name",
            start_at=datetime(2026, 1, 1, tzinfo=UTC),
            end_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        scope=ActivityScope(frozenset(), all_tenants=True),
    )

    assert [row.activity.id for row in page.items] == [activity.id]


async def test_visitor_and_independent_filters_apply_to_page_total_and_export(
    session: AsyncSession,
) -> None:
    shop, other = uuid4(), uuid4()
    moment = datetime(2026, 1, 31, 23, 59, tzinfo=UTC)
    user = User(telegram_user_id=827, telegram_user_name="current_name")
    other_user = User(telegram_user_id=828)
    session.add_all([user, other_user])
    rows = [
        UserActivity(
            created_at=moment,
            tenant_id=owner,
            telegram_bot_id=bot,
            telegram_user_id=telegram,
            username=name,
            action_type="CLICK",
        )
        for owner, bot, telegram, name in (
            (shop, 555, 827, "old_name"),
            (other, 556, 827, "old_name"),
            (shop, 555, 828, "old_name"),
            (shop, 555, 827, "different"),
        )
    ]
    session.add_all(rows)
    await session.flush()
    scope = ActivityScope(frozenset({shop, other}))
    applied = ActivityFilter(
        start_at=moment,
        end_at=datetime(2026, 2, 1, tzinfo=UTC),
        visitor=(shop, 827),
        telegram_user_id=827,
        user_id=user.id,
        username="old_name",
        tenant_ids=frozenset({shop, other}),
    )
    repo = ActivityQueryRepository(session)
    page = await repo.search(applied, scope=scope)
    assert page.total == 1 and [item.activity.id for item in page.items] == [rows[0].id]
    with patch.object(session, "execute", wraps=session.execute) as execute:
        empty = await repo.search(applied, "newest", (moment, rows[0].id), scope=scope)
    execute.assert_awaited_once()
    assert empty.total == 1 and not empty.items
    for conflict in (
        replace(applied, telegram_user_id=828, user_id=None),
        replace(applied, user_id=other_user.id, telegram_user_id=None),
        replace(applied, username="missing_snapshot"),
        replace(applied, tenant_id=other),
        replace(applied, tenant_ids=frozenset({other})),
        replace(applied, bot_id=556),
    ):
        assert (await repo.search(conflict, scope=scope)).total == 0
    async with repo.stream_export(applied, "oldest", scope=scope, limit=1) as result:
        assert [row.id async for row in result] == [rows[0].id]
    assert result.closed


async def test_activity_scope_keyset_tenants_master_and_both_orders(
    session: AsyncSession, tmp_path: Path
) -> None:
    allowed, foreign = uuid4(), uuid4()
    moment = datetime(2026, 1, 15, 12, tzinfo=UTC)
    rows = [
        UserActivity(
            created_at=datetime(2026, 2, 1, tzinfo=UTC) if index < 10 else moment,
            tenant_id=allowed if index < 21 else foreign,
            telegram_bot_id=9000 if index < 21 else 9001,
            telegram_user_id=index,
            username=f"u{index}",
            action_type="OPEN_PRODUCT",
        )
        for index in range(22)
    ]
    master = UserActivity(
        created_at=moment,
        tenant_id=None,
        telegram_bot_id=777,
        telegram_user_id=99,
        username="master",
        action_type="MASTER",
    )
    boundary = UserActivity(
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
        tenant_id=allowed,
        telegram_bot_id=9000,
        telegram_user_id=100,
        username="boundary",
        action_type="BOUNDARY",
    )
    other_master = UserActivity(
        created_at=moment,
        tenant_id=allowed,
        telegram_bot_id=777,
        telegram_user_id=98,
        action_type="MASTER",
    )
    session.add_all([*rows, master, other_master, boundary])
    await session.flush()
    repo = ActivityQueryRepository(session)
    scope = ActivityScope(frozenset({allowed}), master_telegram_bot_id=777, actor_telegram_id=99)
    window = ActivityFilter(
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    newest = await repo.search(window, "newest", scope=scope)
    assert len(newest.items) == 12
    assert {item.activity.created_at.month for item in newest.items} == {1, 2}
    collected = list(newest.items)
    while newest.next_cursor:
        newest = await repo.search(
            window,
            "newest",
            ActivityQueryRepository.decode_cursor(newest.next_cursor),
            scope=scope,
        )
        collected.extend(newest.items)
    ids = [item.activity.id for item in collected]
    assert len(ids) == len(set(ids)) == await repo.count(window, scope=scope) == 22
    assert master.id in ids and other_master.id not in ids
    async with repo.stream_export(window, "newest", scope=scope, limit=100) as result:
        batches = [[row.id for row in batch] async for batch in result.partitions()]
    assert [len(batch) for batch in batches] == [20, 2]
    assert [ident for batch in batches for ident in batch] == ids

    exported = await export_txt(window, repository=repo, logs_dir=tmp_path, scope=scope)
    lines = exported.read_text(encoding="utf-8").splitlines()
    assert [line.rsplit(" | ", 1)[-1] for line in lines] == [f"event_id={ident}" for ident in ids]
    assert await repo.count(window, scope=ActivityScope(frozenset({allowed}), 777)) == 21
    assert all(
        item.activity.tenant_id == allowed or item.activity.telegram_bot_id == 777
        for item in collected
    )
    assert not any(item.activity.telegram_bot_id == 9001 for item in collected)
    oldest = await repo.search(window, "oldest", scope=scope)
    assert oldest.items[0].activity.created_at == moment
    oldest_tail = await repo.search(
        window, "oldest", repo.decode_cursor(oldest.next_cursor or ""), scope=scope
    )
    assert [row.activity.id for row in oldest.items + oldest_tail.items] == ids[::-1]
    first_page = await repo.search(window, "newest", scope=scope)
    second_page = await repo.search(
        window,
        "newest",
        ActivityQueryRepository.decode_cursor(first_page.next_cursor or ""),
        scope=scope,
    )
    backward = await repo.search(
        window,
        "newest",
        (second_page.items[0].activity.created_at, second_page.items[0].activity.id),
        scope=scope,
        backward=True,
    )
    assert backward.next_cursor and backward.previous_cursor is None
    assert [row.activity.id for row in backward.items] == [
        row.activity.id for row in first_page.items
    ]

    without_master = await repo.search(
        window,
        "oldest",
        scope=ActivityScope(frozenset({allowed}), master_telegram_bot_id=777),
    )
    assert all(item.activity.telegram_bot_id != 777 for item in without_master.items)


async def test_visitor_choices_use_shop_scope_before_pagination(session: AsyncSession) -> None:
    allowed, foreign = await _tenant(session), await _tenant(session)
    session.add_all(
        [
            Customer(tenant_id=allowed.id, telegram_user_id=i, username=f"own{i}")
            for i in range(1, 10)
        ]
        + [Customer(tenant_id=foreign.id, telegram_user_id=1, username="foreign")]
    )
    await session.flush()
    options = ActivityFilterOptions(session)
    scope = ActivityScope(frozenset({allowed.id}))
    page, more = await options.page("visitor", scope, allowed.id, 0)
    assert len(page) == 8 and more and page[0].label == "@own1"
    tail, more = await options.page("visitor", scope, allowed.id, 8)
    assert [entry.key for entry in tail] == ["9"] and not more
    assert await options.get("visitor", scope, foreign.id, "1") is None
    assert await options.page("visitor", scope, foreign.id, 0) == ([], False)
    assert await options.get("shop", scope, None, str(foreign.id)) is None
    global_scope = ActivityScope(frozenset(), all_tenants=True)
    visitor = await options.get("visitor", global_scope, foreign.id, "1")
    assert visitor is not None and visitor.label == "@foreign"


async def test_activity_query_explain_uses_bounded_query(session: AsyncSession) -> None:
    repository = ActivityQueryRepository(session)
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(
        month=1 if start.month == 12 else start.month + 1,
        year=start.year + (1 if start.month == 12 else 0),
    )
    filter = ActivityFilter(start_at=start, end_at=end)
    plan = await repository.explain(filter, scope=ActivityScope(frozenset(), all_tenants=True))
    assert plan
    payload = plan[0]
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    assert isinstance(payload, list) and payload

    nodes: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        typed_node = {str(key): value for key, value in node.items()}
        nodes.append(typed_node)
        children = typed_node.get("Plans", [])
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(payload[0].get("Plan", {}))
    assert nodes
    assert any(str(node.get("Relation Name", "")).startswith("user_activity") for node in nodes)
    assert any(node.get("Node Type") == "Limit" for node in nodes)
    rendered = repr(payload).lower()
    assert "created_at" in rendered
    assert start.isoformat()[:10] in rendered and end.isoformat()[:10] in rendered
    relations = {str(node.get("Relation Name")) for node in nodes if node.get("Relation Name")}
    expected_child = f"user_activity_p{start:%Y%m%d}"
    next_child = f"user_activity_p{end:%Y%m%d}"
    assert expected_child in relations
    assert next_child not in relations

    # On the empty/small fixture a sequential scan can be cheaper. Check that
    # PostgreSQL can use the bot+time index when a selective bot is requested.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    filter.bot_id = 987654
    indexed = await repository.explain(filter, scope=ActivityScope(frozenset(), all_tenants=True))
    assert "Index Cond" in repr(indexed) and "telegram_bot_id" in repr(indexed)
