"""Integration coverage for the bounded activity query and PostgreSQL plan."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.models import User
from app.modules.logging.models import UserActivity
from app.modules.logging.query import ActivityFilter, ActivityQueryRepository, ActivityScope

pytestmark = pytest.mark.integration


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


async def test_activity_scope_keyset_tenants_master_and_both_orders(session: AsyncSession) -> None:
    allowed, foreign = uuid4(), uuid4()
    moment = datetime(2026, 1, 15, 12, tzinfo=UTC)
    rows = [
        UserActivity(
            created_at=moment,
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
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        tenant_id=allowed,
        telegram_bot_id=9000,
        telegram_user_id=100,
        username="boundary",
        action_type="BOUNDARY",
    )
    session.add_all([*rows, master, boundary])
    await session.flush()
    repo = ActivityQueryRepository(session)
    scope = ActivityScope(frozenset({allowed}), master_telegram_bot_id=777)
    window = ActivityFilter(
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    newest = await repo.search(window, "newest", scope=scope)
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
    assert len(ids) == len(set(ids)) == 22
    assert all(
        item.activity.tenant_id == allowed or item.activity.telegram_bot_id == 777
        for item in collected
    )
    assert not any(item.activity.telegram_bot_id == 9001 for item in collected)
    oldest = await repo.search(window, "oldest", scope=scope)
    assert oldest.items[0].activity.created_at == moment
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
    assert [row.activity.id for row in backward.items] == [
        row.activity.id for row in first_page.items
    ]

    without_master = await repo.search(
        window,
        "oldest",
        scope=ActivityScope(frozenset({allowed})),
    )
    assert all(item.activity.telegram_bot_id != 777 for item in without_master.items)


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
