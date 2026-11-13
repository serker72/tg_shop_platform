"""Tenant isolation of the shared repository layer (decision 3-a, Stage 1 gap).

Decision 3-a: a full tenant-aware ``BaseRepository`` (``TenantScope`` in the constructor,
``tenant_id`` filter in every read) is built in milestone III together with the tenant
catalogue. What exists now is the contract that makes today's layer safe:

* :class:`BaseRepository` carries no tenant filter of its own - a platform-wide read;
* a tenant-scoped ``list()`` **must** pass ``TenantScope`` *and* the ``tenant_of`` accessor;
* passing only one of them is refused (``tenant_scope_required``) - a read that *looks*
  tenant-scoped can never silently fall back to "all tenants";
* every returned row is verified with :meth:`TenantScope.ensure_owns`, a foreign row raises
  :class:`TenantIsolationError` (``tenant_mismatch``) before the caller sees it.

The test runs the real :class:`BaseRepository` over the real ``Tenant`` model of the test
database (savepoint session, rolled back afterwards).
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.models import Tenant
from app.shared_kernel.domain.exceptions import TenantIsolationError
from app.shared_kernel.domain.tenant import TenantScope
from app.shared_kernel.infrastructure.base_repository import (
    MAX_SCOPED_LIMIT,
    BaseRepository,
    ScopedPage,
)

pytestmark = [pytest.mark.integration, pytest.mark.req("R2")]

#: ``BaseRepository(session, Tenant)`` + how to read ``tenant_id`` of a ``Tenant`` row.
#: ``Tenant`` has no ``tenant_id`` column (it *is* the tenant), so its own primary key is
#: used - the same rule applies to every tenant-scoped model of milestone III.
TENANT_OF = lambda tenant: tenant.id  # noqa: E731 - short lambda in a test

#: ``Tenant`` has no ``tenant_id`` column (it *is* the tenant), so the primary key is the
#: SQL filter column for a scoped read of tenants.
TENANT_COLUMN = "id"


async def make_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = Tenant(name=name, slug=f"shop-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


def test_repository_stays_tenant_agnostic() -> None:
    """No hidden tenant state: the constructor takes only ``session`` and ``model``."""
    signature = inspect.signature(BaseRepository.__init__)
    assert list(signature.parameters)[1:] == ["session", "model"]
    assert "tenant_id" not in inspect.getsource(BaseRepository._base_stmt)
    assert "tenant_of" in inspect.signature(BaseRepository.list).parameters


async def test_tenant_scoped_read_filters_in_sql(session: AsyncSession) -> None:
    """Two shops, one scope: the SQL filter keeps foreign rows out of the page.

    The filter is applied before the window, so a foreign row cannot occupy a slot: the
    owned row is returned and ``truncated`` reflects *this* tenant only. ``ensure_owns``
    stays as a second line of defence for rows that do reach the page.
    """
    own = await make_tenant(session, "Own shop")
    foreign = await make_tenant(session, "Foreign shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)

    platform = await repository.list()
    assert not isinstance(platform, ScopedPage)
    assert {own.id, foreign.id} <= {tenant.id for tenant in platform}

    scoped = await repository.list(
        scope=TenantScope(tenant_id=own.id),
        tenant_of=TENANT_OF,
        tenant_column=TENANT_COLUMN,
        limit=50,
    )
    assert isinstance(scoped, ScopedPage)
    assert [tenant.id for tenant in scoped.rows] == [own.id]
    assert scoped.truncated is False


async def test_scope_without_accessor_is_refused(session: AsyncSession) -> None:
    """A read that looks tenant-scoped never degrades into "all tenants"."""
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(scope=TenantScope(tenant_id=uuid.uuid4()))
    assert caught.value.code == "tenant_scope_required"

    with pytest.raises(TenantIsolationError) as caught:
        # ``tenant_of`` alone cannot filter in SQL -> the page window would be wrong.
        await repository.list(tenant_of=TENANT_OF)
    assert caught.value.code == "tenant_scope_required"

    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(scope=TenantScope(tenant_id=uuid.uuid4()), tenant_of=TENANT_OF)
    assert caught.value.code == "tenant_scope_required"

    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(
            scope=TenantScope(tenant_id=uuid.uuid4()),
            tenant_of=TENANT_OF,
            tenant_column="no_such_column",
        )
    assert caught.value.code == "tenant_column_unknown"


async def test_foreign_row_raises_tenant_isolation_error(session: AsyncSession) -> None:
    """The accessor still guards: a mismatching ``tenant_id`` refuses the whole page."""
    await make_tenant(session, "Foreign shop")
    own = await make_tenant(session, "Own shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(
            scope=TenantScope(tenant_id=own.id),
            # The SQL filter passes the own row; the accessor (second line of defence)
            # reports another tenant for it, so the page is refused.
            tenant_of=lambda _: uuid.UUID(int=0),
            tenant_column=TENANT_COLUMN,
        )
    assert caught.value.code == "tenant_mismatch"
    # The message never leaks either id.
    assert str(own.id) not in str(caught.value)


async def test_row_without_tenant_id_is_refused(session: AsyncSession) -> None:
    shop = await make_tenant(session, "Shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(
            scope=TenantScope(tenant_id=shop.id),
            tenant_of=lambda _: None,
            tenant_column=TENANT_COLUMN,
        )
    assert caught.value.code == "tenant_mismatch"


async def test_unscoped_list_is_platform_wide(session: AsyncSession) -> None:
    """Without a scope ``list()`` is the platform-wide read: every live tenant is returned."""
    own = await make_tenant(session, "Mine")
    foreign = await make_tenant(session, "Theirs")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    page = await repository.list()
    assert not isinstance(page, ScopedPage)
    ids = {tenant.id for tenant in page}
    assert {own.id, foreign.id} <= ids


def test_scope_rejects_non_uuid() -> None:
    """A typo cannot widen access: only a real ``UUID`` is accepted."""
    with pytest.raises(TenantIsolationError) as caught:
        TenantScope(tenant_id="not-a-uuid")  # type: ignore[arg-type]
    assert caught.value.code == "tenant_id_invalid"


async def test_scoped_read_reports_truncation_instead_of_hiding_it(
    session: AsyncSession,
) -> None:
    """RC-3-A: a scoped page is capped *and* says so - ``truncated`` is the signal."""
    own = await make_tenant(session, "Own shop")
    await make_tenant(session, "Second shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    scoped = await repository.list(
        scope=TenantScope(tenant_id=own.id),
        tenant_of=TENANT_OF,
        tenant_column=TENANT_COLUMN,
        limit=10 * MAX_SCOPED_LIMIT,
    )
    assert isinstance(scoped, ScopedPage)
    assert scoped.limit == MAX_SCOPED_LIMIT
    assert len(scoped.rows) <= MAX_SCOPED_LIMIT
    # The foreign shop does not consume a slot: only ``own`` is in the page, nothing left.
    assert [tenant.id for tenant in scoped.rows] == [own.id]
    assert scoped.truncated is False


async def test_foreign_rows_in_the_window_do_not_hide_own_rows(session: AsyncSession) -> None:
    """Final-review item 3: a foreign row in front of the window must not steal a slot.

    A foreign shop is created first, so it is older and would sort first under
    ``created_at, id``. With the SQL filter the page still starts with the owned row; without
    it the owned row would be pushed out of a ``limit=1`` window and ``truncated`` would
    report the foreign row.
    """
    foreign = await make_tenant(session, "Foreign first")
    own = await make_tenant(session, "Own second")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)

    page = await repository.list(
        scope=TenantScope(tenant_id=own.id),
        tenant_of=TENANT_OF,
        tenant_column=TENANT_COLUMN,
        limit=1,
    )
    assert isinstance(page, ScopedPage)
    assert [tenant.id for tenant in page.rows] == [own.id]
    assert page.truncated is False
    assert foreign.id not in {tenant.id for tenant in page.rows}


async def test_unscoped_read_returns_a_plain_sequence(session: AsyncSession) -> None:
    """RC-3-A: only a scoped read changes shape; the platform-wide read stays a sequence."""
    await make_tenant(session, "Shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    page = await repository.list()
    assert not isinstance(page, ScopedPage)
    assert len(page) >= 1


async def test_tenant_column_must_be_a_real_column(session: AsyncSession) -> None:
    """Only a mapped column filters: a relationship/property/method name is refused."""
    own = await make_tenant(session, "Own shop")
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    for name in ("no_such_column", "__init__", "metadata"):
        with pytest.raises(TenantIsolationError) as caught:
            await repository.list(
                scope=TenantScope(tenant_id=own.id), tenant_of=TENANT_OF, tenant_column=name
            )
        assert caught.value.code == "tenant_column_unknown"


async def test_tenant_column_without_scope_is_refused(session: AsyncSession) -> None:
    """``tenant_column`` alone has no tenant to filter by: never a silent platform read."""
    repository: BaseRepository[Tenant] = BaseRepository(session, Tenant)
    with pytest.raises(TenantIsolationError) as caught:
        await repository.list(tenant_column=TENANT_COLUMN)
    assert caught.value.code == "tenant_scope_required"
