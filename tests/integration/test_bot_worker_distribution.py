"""Task 11 acceptance with real PostgreSQL, Redis DB 15 and RabbitMQ."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.exceptions import DeliveryError
from aiogram import Bot as TelegramBot
from aiogram import Dispatcher
from aiohttp.test_utils import TestClient, TestServer
from alembic import command as alembic
from dishka import AsyncContainer, Provider, Scope, make_async_container
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange
from faststream_outbox import OutboxBroker
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.observability.broker import build_rabbit_broker
from app.core.observability.health import HealthChecker, HeartbeatRegistry
from app.core.observability.http_server import create_app
from app.core.observability.metrics import build_metrics
from app.core.process import ServiceContext, serve_components
from app.entrypoints.bot_worker import commands, scheduler
from app.entrypoints.bot_worker.app import bot_worker_component
from app.modules.bot_manager import runtime, tasks
from app.modules.bot_manager.commands import BotCommand, BotCommandKind
from app.modules.bot_manager.lease import BotLease
from app.modules.bot_manager.models import Bot, Proxy
from app.modules.bot_manager.proxy_health import (
    OUTAGE_KEY,
    ProxyHealthTracker,
    StrikeLeaseLostError,
    fail_key,
    ok_key,
    strike_key,
)
from app.modules.bot_manager.runtime import BotSupervisor
from app.modules.bot_manager.security import token_hash
from app.modules.bot_manager.worker import BotWorker
from app.modules.iam.models import Tenant, User
from app.modules.logging.buffer import LogBuffer
from app.modules.logging.events import ActivityEvent
from app.modules.logging.ioc import LoggingProvider
from app.modules.logging.models import UserActivity
from app.modules.logging.ports import ActivityLogger
from app.shared_kernel.infrastructure.outbox import SharedOutboxRelay, build_outbox_broker
from tests.integration._guards import (
    assert_test_database,
    guarded_rabbit_broker,
    guarded_rabbit_url,
)
from tests.integration.conftest import Query, run_alembic
from tests.integration.test_tenant_bots_autostop import ACTOR, Permissions, services
from tests.integration.test_tenant_bots_autostop import commands as pending_commands
from tests.unit.test_process import sigterm_when

pytestmark = [pytest.mark.integration, pytest.mark.req("R-II.9")]
TOKEN = "123456:" + "A" * 35
NEW_TOKEN = "123456:" + "B" * 35


class Telegram:
    """One successful getUpdates per session, then a cancellable long poll."""

    def __init__(self) -> None:
        self.started: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self.failures: dict[str, asyncio.Event] = {}
        self.open: set[object] = set()
        self.max_open = 0

    def session(self, proxy: str | None) -> Mock:
        identity = object()
        self.open.add(identity)
        self.max_open = max(self.max_open, len(self.open))

        async def invoke(bot: TelegramBot, *_: Any, **__: Any) -> list[Any]:
            if invoke_mock.call_count == 1:
                self.started.put_nowait((bot.token, proxy))
                return []
            if proxy is not None:
                await self.failures.setdefault(proxy, asyncio.Event()).wait()
                raise ConnectionError("simulated Telegram network failure")
            await asyncio.Future()
            raise AssertionError("blocked long poll unexpectedly returned")

        async def close() -> None:
            self.open.remove(identity)

        invoke_mock = AsyncMock(side_effect=invoke, close=AsyncMock(side_effect=close))
        return invoke_mock

    async def next(self, token: str = TOKEN, proxy: str | None = None) -> None:
        assert await asyncio.wait_for(self.started.get(), 5) == (token, proxy)


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Telegram:
    fake = Telegram()
    monkeypatch.setattr(runtime, "build_bot_session", fake.session)
    monkeypatch.setattr(runtime, "probe_handshake", AsyncMock())
    return fake


async def eventually(predicate: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(8):
        while not await predicate():  # noqa: ASYNC110 - observes external Redis/SQL state
            await asyncio.sleep(0.025)


class Fleet:
    def __init__(self, engine: AsyncEngine, redis: Redis, settings: Settings) -> None:
        self.engine, self.redis, self.settings = engine, redis, settings
        self.db = async_sessionmaker(engine, expire_on_commit=False)
        self.workers: list[BotWorker] = []
        self.supervisors: list[BotSupervisor] = []
        self.proxies: list[UUID] = []
        self.metrics = build_metrics(component="bot_worker")
        self.tenant_id: UUID
        self.bot_id: UUID

    @property
    def lease_key(self) -> str:
        return f"bot:lease:{self.bot_id}"

    async def seed(self, *, active: bool = True) -> None:
        async with self.db.begin() as session:
            tenant = Tenant(name="Worker test shop", slug="worker-shop", is_active=True)
            session.add_all([tenant, User(telegram_user_id=ACTOR, is_active=True)])
            await session.flush()
            bot = Bot(
                tenant_id=tenant.id,
                telegram_bot_id=123456,
                encrypted_token=TOKEN,
                token_hash=token_hash(TOKEN),
                is_active=active,
                is_deleted=False,
                status="stopped",
            )
            session.add(bot)
            await session.flush()
            self.tenant_id, self.bot_id = tenant.id, bot.id

    def config(self, *, use_proxy: bool = False, ttl: int = 30) -> Settings:
        return self.settings.model_copy(
            update={
                "bot_worker": self.settings.bot_worker.model_copy(
                    update={
                        "lease_ttl_seconds": ttl,
                        "reconcile_interval_seconds": 60,
                    }
                ),
                "proxy": self.settings.proxy.model_copy(
                    update={
                        "tenants_use_proxy": use_proxy,
                        "proxy_failure_threshold": 1,
                        "proxy_strikes_to_quarantine": 1,
                    }
                ),
                "scheduler": self.settings.scheduler.model_copy(
                    update={"scheduler_enabled": False}
                ),
            }
        )

    def worker(self, **options: Any) -> BotWorker:
        cfg = self.config(**options)
        sup = BotSupervisor.from_settings(cfg, metrics=build_metrics(component="bot_worker"))
        worker = BotWorker(
            cfg,
            self.redis,
            self.db,
            sup,
            Dispatcher(disable_fsm=True),
            owner=f"replica-{len(self.workers) + 1}:123",
        )
        self.workers.append(worker)
        self.supervisors.append(sup)
        return worker

    def command(self, kind: BotCommandKind) -> BotCommand:
        return BotCommand(
            kind=kind,
            bot_id=self.bot_id,
            command_id=uuid4(),
            idempotency_key=uuid4(),
            issued_at=datetime.now(UTC),
            issued_by=ACTOR,
        )

    async def set_bot(self, *, active: bool | None = None, token: str | None = None) -> None:
        async with self.db.begin() as session:
            bot = await session.get(Bot, self.bot_id)
            assert bot is not None
            if active is not None:
                bot.is_active = active
            if token is not None:
                bot.encrypted_token, bot.token_hash = token, token_hash(token)

    async def snapshot(self) -> tuple[str, str | None, UUID | None]:
        async with self.db() as session:
            bot = await session.get(Bot, self.bot_id)
            assert bot is not None
            return bot.status, bot.last_error, bot.proxy_id

    async def running(self) -> bool:
        return (await self.snapshot())[0] == "running"

    async def proxy(self) -> tuple[UUID, str]:
        url = f"socks5://proxy-{len(self.proxies)}.invalid:1080"
        async with self.db.begin() as session:
            proxy = Proxy(url=url, url_hash=uuid4().hex)
            session.add(proxy)
            await session.flush()
            self.proxies.append(proxy.id)
            return proxy.id, url

    def context(self, *, use_proxy: bool = False) -> ServiceContext:
        deps: dict[object, object] = {
            Redis: self.redis,
            AsyncEngine: self.engine,
            OutboxBroker: build_outbox_broker(self.engine),
            ActivityLogger: Mock(spec=ActivityLogger),
            async_sessionmaker[AsyncSession]: self.db,
        }
        container = AsyncMock(spec=AsyncContainer)
        container.get.side_effect = deps.__getitem__
        return ServiceContext(
            name="bot_worker",
            settings=self.config(use_proxy=use_proxy),
            container=container,
            metrics=self.metrics,
            readiness_registry=HeartbeatRegistry(),
        )


@pytest.fixture
async def fleet(
    clean_db: AsyncEngine, redis_client: Redis, settings: Settings, telegram: Telegram
) -> AsyncIterator[Fleet]:
    assert_test_database(settings)
    assert (await redis_client.client_info())["db"] == 15
    harness = Fleet(clean_db, redis_client, settings)
    await harness.seed()
    try:
        yield harness
    finally:
        try:
            await asyncio.gather(*(worker.shutdown() for worker in harness.workers))
        finally:
            await redis_client.unlink(
                harness.lease_key,
                f"{harness.lease_key}:generation",
                *(
                    key(proxy)
                    for proxy in harness.proxies
                    for key in (ok_key, fail_key, strike_key)
                ),
            )
            assert not telegram.open


def build_guarded_broker(settings: Settings, **kwargs: Any) -> RabbitBroker:
    return guarded_rabbit_broker(build_rabbit_broker(settings, **kwargs), settings)


@pytest.fixture
async def rabbit(fleet: Fleet, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RabbitExchange]:
    """Scope commands and the always-subscribed scheduler consumer to this test."""
    url = guarded_rabbit_url(fleet.settings)
    monkeypatch.setattr(scheduler, "build_rabbit_broker", build_guarded_broker)
    name = f"test.distribution.{uuid4().hex}"
    scheduler_queue = f"{name}.proxy.rehabilitate"
    monkeypatch.setattr(tasks, "task_queue", lambda _: scheduler_queue)
    dlq = f"{name}.dlq"
    exchange = RabbitExchange(name, type=ExchangeType.FANOUT, durable=True)
    monkeypatch.setattr(commands, "COMMAND_QUEUE", name)
    monkeypatch.setattr(commands, "COMMAND_DLQ", dlq)
    monkeypatch.setattr(
        commands,
        "COMMAND_QUEUE_ARGUMENTS",
        {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": dlq,
        },
    )
    monkeypatch.setattr(commands, "BOT_COMMAND_FANOUT", exchange)
    monkeypatch.setattr(scheduler, "BOT_COMMAND_FANOUT", exchange)
    monkeypatch.setattr(scheduler, "BOT_COMMAND_EXCHANGE", name)
    monkeypatch.setattr(
        scheduler, "SharedOutboxRelay", lambda **kw: SharedOutboxRelay(**kw, queues=(name,))
    )
    async with await aio_pika.connect(url) as conn:
        channel = await conn.channel()
        try:
            yield exchange
        finally:
            await channel.queue_delete(dlq)
            await channel.queue_delete(scheduler_queue)
            await channel.exchange_delete(name)


async def test_one_owner_and_immediate_graceful_handoff(fleet: Fleet, telegram: Telegram) -> None:
    first, second = fleet.worker(), fleet.worker()
    await asyncio.gather(first.reconcile(strict=True), second.reconcile(strict=True))
    await telegram.next()
    await eventually(fleet.running)
    owner = await fleet.redis.get(fleet.lease_key)
    assert owner in {first.owner.encode(), second.owner.encode()}
    winner, standby = (first, second) if owner == first.owner.encode() else (second, first)
    assert [s.running_ids() for s in fleet.supervisors] == [
        {fleet.bot_id} if worker is winner else set() for worker in (first, second)
    ]
    assert await fleet.redis.pttl(fleet.lease_key) > 0
    await winner.shutdown()
    assert not await fleet.redis.exists(fleet.lease_key, f"{fleet.lease_key}:generation")
    assert not telegram.open
    await standby.reconcile(strict=True)
    await telegram.next()
    assert await fleet.redis.get(fleet.lease_key) == standby.owner.encode()
    assert telegram.max_open == 1


async def test_crash_does_not_release_lease_and_standby_waits_for_redis_ttl(
    fleet: Fleet, telegram: Telegram
) -> None:
    first, standby = fleet.worker(ttl=20), fleet.worker()
    renew = asyncio.create_task(first.renew_loop())
    try:
        await first.reconcile(strict=True)
        await telegram.next()
        await eventually(fleet.running)
        # Observe a genuine TTL extension before simulating process loss.
        old_ttl = await fleet.redis.pttl(fleet.lease_key)

        async def renewed() -> bool:
            nonlocal old_ttl
            ttl = await fleet.redis.pttl(fleet.lease_key)
            grew = ttl > old_ttl
            old_ttl = ttl
            return grew

        await eventually(renewed)
    finally:
        renew.cancel()
        with suppress(asyncio.CancelledError):
            await renew
    # In-process crash simulation: stop the polling tasks, but never call the
    # worker's shutdown/release path until after standby has acquired the lease.
    await fleet.supervisors[0].stop_all()
    assert not telegram.open
    assert await fleet.redis.get(fleet.lease_key) == first.owner.encode()
    await standby.reconcile(strict=True)
    assert not fleet.supervisors[1].running_ids()
    assert telegram.started.empty()
    assert await fleet.redis.pttl(fleet.lease_key) > 1_000
    async with fleet.redis.pipeline(transaction=True) as pipe:
        pipe.pexpire(fleet.lease_key, 250)
        pipe.pexpire(f"{fleet.lease_key}:generation", 250)
        assert await pipe.execute() == [True, True]

    async def expired() -> bool:
        return not await fleet.redis.exists(fleet.lease_key, f"{fleet.lease_key}:generation")

    await eventually(expired)
    await standby.reconcile(strict=True)
    await telegram.next()
    await first.shutdown()  # stale cleanup cannot release the successor
    assert await fleet.redis.get(fleet.lease_key) == standby.owner.encode()
    assert telegram.max_open == 1


async def test_fanout_non_owner_cannot_swallow_restart_or_stop(
    fleet: Fleet, telegram: Telegram, rabbit: RabbitExchange, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, non_owner = fleet.worker(), fleet.worker()
    await owner.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    seen: asyncio.Queue[tuple[str, UUID]] = asyncio.Queue()
    standby_done = asyncio.Event()
    real_restart = non_owner.restart

    async def standby_restart(command: BotCommand) -> None:
        await real_restart(command)
        seen.put_nowait(("standby", command.command_id))
        standby_done.set()

    async def owner_restart(command: BotCommand) -> None:
        await asyncio.wait_for(standby_done.wait(), 3)
        await original_restart(command)
        seen.put_nowait(("owner", command.command_id))

    original_restart = owner.restart
    monkeypatch.setattr(non_owner, "restart", standby_restart)
    monkeypatch.setattr(owner, "restart", owner_restart)
    async with AsyncExitStack() as stack:
        brokers = []
        for worker in (non_owner, owner):
            broker = await stack.enter_async_context(build_guarded_broker(fleet.settings))
            await commands.declare_bot_command_topology(broker)
            commands.register_bot_command_consumer(
                broker, worker, commands.InMemoryCommandDeduplicator()
            )
            await broker.start()
            brokers.append(broker)
        await fleet.set_bot(token=NEW_TOKEN)
        restart = fleet.command("RESTART_BOT")
        await brokers[0].publish(restart.model_dump(mode="json"), exchange=rabbit, mandatory=True)
        assert await asyncio.wait_for(seen.get(), 5) == ("standby", restart.command_id)
        assert await asyncio.wait_for(seen.get(), 5) == ("owner", restart.command_id)
        await telegram.next(NEW_TOKEN)
        await eventually(fleet.running)
        assert await fleet.redis.get(fleet.lease_key) == owner.owner.encode()
        assert not fleet.supervisors[1].running_ids()
        await fleet.set_bot(active=False)
        stop = fleet.command("STOP_BOT")
        await brokers[1].publish(stop.model_dump(mode="json"), exchange=rabbit, mandatory=True)

        async def stopped() -> bool:
            return not await fleet.redis.exists(fleet.lease_key) and not telegram.open

        await eventually(stopped)
        assert await fleet.snapshot() == ("stopped", "bot_inactive", None)
        assert telegram.max_open == 1


async def test_reconcile_reloads_rotated_token_without_restart_delivery(
    fleet: Fleet, telegram: Telegram
) -> None:
    worker = fleet.worker()
    await worker.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    await fleet.set_bot(token=NEW_TOKEN)
    await worker.reconcile(strict=True)
    await telegram.next(NEW_TOKEN)
    await eventually(fleet.running)
    assert telegram.max_open == 1
    assert await fleet.redis.get(fleet.lease_key) == worker.owner.encode()


@pytest.mark.parametrize("recover", ["delivery", "reconcile", "handoff"])
async def test_same_token_restart_revision_applied_once(
    fleet: Fleet, telegram: Telegram, recover: str
) -> None:
    owner = fleet.worker()
    await owner.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    async with fleet.db() as writer:
        _, control, _ = services(writer, fleet.engine)
        control.permissions = Permissions(bot_scope=fleet.tenant_id)
        bot = await control.restart(fleet.bot_id, ACTOR, tenant_id=fleet.tenant_id)
        assert (bot.restart_revision, bot.token_hash) == (1, token_hash(TOKEN))
        (command,) = await pending_commands(writer)
        assert (command.kind, command.bot_id) == ("RESTART_BOT", fleet.bot_id)
    if recover == "delivery":
        await owner.restart(command)
    elif recover == "reconcile":
        await owner.reconcile(strict=True)
    else:
        await owner.shutdown()
        owner = fleet.worker()
        await owner.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    sessions = telegram.open.copy()
    await owner.restart(command)
    await owner.restart(command)
    assert telegram.open == sessions
    assert telegram.started.empty()


async def test_restart_outbox_and_revision_roll_back_after_audit_failure(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fleet.db() as writer:
        _, control, _ = services(writer, fleet.engine)
        control.permissions = Permissions(bot_scope=fleet.tenant_id)

        async def fail_after_enqueue(*_: object) -> None:
            (cmd,) = await pending_commands(writer)
            assert cmd.kind == "RESTART_BOT"
            raise RuntimeError("forced audit failure")

        monkeypatch.setattr(control, "_audit", fail_after_enqueue)
        with pytest.raises(RuntimeError, match="forced audit failure"):
            await control.restart(fleet.bot_id, ACTOR, tenant_id=fleet.tenant_id)
    async with fleet.db() as reader:
        bot = await reader.get(Bot, fleet.bot_id)
        assert bot is not None and (bot.restart_revision, bot.status) == (0, "stopped")
        assert await pending_commands(reader) == []


async def test_stop_then_start_before_delivery_keeps_polling_session(
    fleet: Fleet, telegram: Telegram
) -> None:
    worker = fleet.worker()
    await worker.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    sessions = telegram.open.copy()
    async with fleet.db() as writer:
        _, control, _ = services(writer, fleet.engine)
        control.permissions = Permissions(bot_scope=fleet.tenant_id)
        await control.stop(fleet.bot_id, ACTOR, tenant_id=fleet.tenant_id)
        await control.start(fleet.bot_id, ACTOR, tenant_id=fleet.tenant_id)
        queued = {cmd.kind: cmd for cmd in await pending_commands(writer)}
    assert (await fleet.snapshot())[0] == "starting"
    await worker.stop(queued["STOP_BOT"])
    await worker.start(queued["START_BOT"])
    assert await fleet.running()
    assert telegram.open == sessions
    assert telegram.started.empty()


@pytest.mark.req("R-II.16")
async def test_standby_repairs_orphan_status_on_inactive_tenant(
    fleet: Fleet, telegram: Telegram
) -> None:
    owner, standby = fleet.worker(), fleet.worker()
    await owner.reconcile(strict=True)
    await telegram.next()
    await eventually(fleet.running)
    await fleet.redis.unlink(fleet.lease_key, f"{fleet.lease_key}:generation")
    await fleet.supervisors[0].stop_all()
    async with fleet.db() as writer:
        admin, _, _ = services(writer, fleet.engine)
        await admin.deactivate(ACTOR, fleet.tenant_id)
    assert await fleet.running()
    await standby.reconcile(strict=True)
    assert await fleet.snapshot() == ("stopped", "tenant_inactive", None)
    assert not telegram.open


@pytest.mark.req("R-II.16")
@pytest.mark.parametrize("delete", [False, True], ids=["deactivate-activate", "delete-restore"])
async def test_reconcile_repairs_lost_tenant_stop(
    fleet: Fleet, telegram: Telegram, delete: bool
) -> None:
    worker = fleet.worker(use_proxy=delete)
    old_id, old_url = await fleet.proxy() if delete else (None, None)
    await worker.reconcile(strict=True)
    await telegram.next(proxy=old_url)
    await eventually(fleet.running)
    reason = "tenant_deleted" if delete else "tenant_inactive"
    async with fleet.db() as writer:
        admin, control, _ = services(writer, fleet.engine)
        await admin.deactivate(ACTOR, fleet.tenant_id)
        if delete:
            await admin.soft_delete(ACTOR, fleet.tenant_id)
            await control.stop_tenant(fleet.tenant_id, reason="tenant_deleted", actor=ACTOR)
            queued = await pending_commands(writer)
            assert [(cmd.kind, cmd.bot_id) for cmd in queued] == [("STOP_BOT", fleet.bot_id)]
            assert await fleet.snapshot() == ("stopped", reason, None)
            assert old_id is not None
            # Simulate the old proxy having become unavailable while the shop was deleted.
            async with fleet.db.begin() as session:
                proxy = await session.get(Proxy, old_id)
                assert proxy is not None
                proxy.status = "banned"
        # No consumer or relay delivers STOP; polling is still in progress.
        assert len(telegram.open) == 1
        await worker.reconcile(strict=True)
        assert not telegram.open
        assert not await fleet.redis.exists(fleet.lease_key)
        assert await fleet.snapshot() == ("stopped", reason, None)
        async with fleet.db() as reader:
            bot = await reader.get(Bot, fleet.bot_id)
            assert bot is not None and bot.is_active and not bot.is_deleted
        if delete:
            new_id, new_url = await fleet.proxy()
            await admin.restore(ACTOR, fleet.tenant_id)
        else:
            new_id, new_url = None, None
            await admin.activate(ACTOR, fleet.tenant_id)
        await worker.reconcile(strict=True)
        await telegram.next(proxy=new_url)
        await eventually(fleet.running)
        assert await fleet.snapshot() == ("running", None, new_id)


async def test_component_startup_no_proxy_quarantine_and_shutdown(
    fleet: Fleet, telegram: Telegram, rabbit: RabbitExchange
) -> None:
    ctx = fleet.context(use_proxy=True)
    ctx.settings.bot_worker.reconcile_interval_seconds = 2
    readiness = ctx.readiness_registry
    assert readiness is not None
    metric = "bots_no_proxy_total"
    async with bot_worker_component(ctx):
        assert readiness.readiness[scheduler.BOT_RUNTIME_READINESS].ready
        assert await fleet.snapshot() == ("error", "no_proxy_available", None)
        assert ctx.metrics.registry.get_sample_value(metric) == 1
        assert not await fleet.redis.exists(fleet.lease_key)
        assert telegram.started.empty()
        old_id, old_url = await fleet.proxy()
        await telegram.next(proxy=old_url)  # periodic reconcile, without START delivery
        await eventually(fleet.running)
        new_id, new_url = await fleet.proxy()
        # A successful request on another active proxy distinguishes this failure
        # from a Telegram-wide outage; only the transport is simulated.
        await ProxyHealthTracker(fleet.redis, window_seconds=60).mark_ok(new_id)
        telegram.failures.setdefault(old_url, asyncio.Event()).set()
        await telegram.next(proxy=new_url)
        await eventually(fleet.running)
        async with fleet.db() as session:
            proxy = await session.get(Proxy, old_id)
            assert proxy is not None and (proxy.status, proxy.strike_count) == ("quarantine", 1)
        assert await fleet.snapshot() == ("running", None, new_id)
        assert ctx.metrics.registry.get_sample_value(metric) == 1
        assert telegram.max_open == 1
        async with await aio_pika.connect(guarded_rabbit_url(fleet.settings)) as connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchange = await channel.get_exchange(rabbit.name)
            dlq = await channel.get_queue(f"{rabbit.name}.dlq")
            poison = b'{"kind":"invalid"}'
            await exchange.publish(
                aio_pika.Message(poison, content_type="application/json"), routing_key=""
            )

            async def dead_lettered() -> bool:
                message = await dlq.get(fail=False)
                if message is None:
                    return False
                async with message.process():
                    assert message.body == poison
                    assert message.headers["x-first-death-reason"] == "rejected"
                return True

            await eventually(dead_lettered)
    assert not readiness.readiness[scheduler.BOT_RUNTIME_READINESS].ready
    assert not await fleet.redis.exists(fleet.lease_key)
    assert not telegram.open
    assert await fleet.snapshot() == ("stopped", None, new_id)
    async with await aio_pika.connect(guarded_rabbit_url(fleet.settings)) as connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        exchange = await channel.get_exchange(rabbit.name)
        with pytest.raises(DeliveryError):
            await exchange.publish(aio_pika.Message(b"no subscribers"), routing_key="")


@pytest.mark.parametrize("change", ["stop", "revision"])
async def test_rejected_starting_does_not_poll(
    fleet: Fleet, telegram: Telegram, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    worker = fleet.worker()
    original = worker._write_status
    entering, resume = asyncio.Event(), asyncio.Event()
    attempts: list[str] = []

    async def pause_starting(slot: Any, status: str, error: str | None) -> bool:
        attempts.append(status)
        if status == "starting":
            entering.set()
            await resume.wait()
        return await original(slot, status, error)

    monkeypatch.setattr(worker, "_write_status", pause_starting)
    await worker.reconcile(strict=True)
    async with asyncio.timeout(5):
        await entering.wait()
    try:
        async with fleet.db.begin() as session:
            bot = await session.get(Bot, fleet.bot_id)
            assert bot is not None
            if change == "stop":
                bot.is_active = False
            else:
                bot.restart_revision += 1
    finally:
        resume.set()

    async def finished() -> bool:
        return not fleet.supervisors[0].running_ids()

    await eventually(finished)
    assert attempts == ["starting"]
    assert telegram.started.empty() and telegram.max_open == 0


async def test_proxy_marks_reject_previous_generation_for_same_owner(fleet: Fleet) -> None:
    proxy_id, _ = await fleet.proxy()
    old = BotLease(fleet.redis, fleet.bot_id, "same:123", 30)
    current = BotLease(fleet.redis, fleet.bot_id, old.owner, 30)
    tracker = ProxyHealthTracker(fleet.redis, window_seconds=60, ok_throttle_seconds=0)
    assert await old.acquire()
    assert await old.release()
    assert await current.acquire()
    try:
        with pytest.raises(StrikeLeaseLostError):
            await tracker.try_acquire_strike(proxy_id, lease=old)
        assert not await fleet.redis.exists(strike_key(proxy_id))
        token = await tracker.try_acquire_strike(proxy_id, lease=current)
        assert token is not None
        assert await tracker.try_acquire_strike(proxy_id, lease=current) is None
        assert await tracker.release_strike(proxy_id, token)
        assert not await tracker.set_outage(lease=old)
        assert not await fleet.redis.exists(OUTAGE_KEY)
        assert await tracker.set_outage(lease=current)
        assert not await tracker.mark_ok(proxy_id, lease=old)
        assert not await tracker.mark_fail(proxy_id, lease=old)
        assert await fleet.redis.exists(OUTAGE_KEY)
        assert not await fleet.redis.exists(ok_key(proxy_id), fail_key(proxy_id))
        assert await tracker.mark_ok(proxy_id, lease=current)
        assert not await fleet.redis.exists(OUTAGE_KEY)
        assert await tracker.mark_fail(proxy_id, lease=current)
        assert await fleet.redis.exists(ok_key(proxy_id), fail_key(proxy_id)) == 2
    finally:
        await fleet.redis.delete(OUTAGE_KEY)
        await current.release()


async def test_rabbit_reconnect_restores_readiness_and_delivers_stop(
    fleet: Fleet, telegram: Telegram, rabbit: RabbitExchange, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = fleet.context()
    registry = ctx.readiness_registry
    assert registry is not None
    brokers: list[RabbitBroker] = []
    readiness_on_disconnect: list[bool] = []
    disconnected_http: list[int] = []

    def capture(settings: Settings, **hooks: Any) -> RabbitBroker:
        on_close = hooks.pop("on_close")

        async def disconnected(connection: object, exc: BaseException | None) -> None:
            on_close(connection, exc)
            if exc is not None or not readiness_on_disconnect:
                readiness_on_disconnect.append(
                    registry.readiness[scheduler.BOT_RUNTIME_READINESS].ready
                )
            if not disconnected_http:
                async with http.get("/health/ready") as response:
                    disconnected_http.append(response.status)

        broker = build_guarded_broker(settings, on_close=disconnected, **hooks)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(scheduler, "build_rabbit_broker", capture)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(bot_worker_component(ctx))
        checker = HealthChecker(registry.checks(fleet.redis, ttl=30), timeout=1)
        app = create_app(checker, ctx.metrics.registry, ready_cache_ttl=300)
        http = await stack.enter_async_context(TestClient(TestServer(app)))
        async with http.get("/health/ready") as response:
            assert response.status == 200
        await telegram.next()
        await eventually(fleet.running)
        gate = registry.readiness[scheduler.BOT_RUNTIME_READINESS]
        assert gate.ready
        (broker,) = brokers
        connection = await broker.connect()
        transport = connection.transport
        assert transport is not None
        await transport.connection.close()

        async def recovered() -> bool:
            return bool(readiness_on_disconnect and gate.ready)

        await eventually(recovered)
        assert readiness_on_disconnect == [False]
        assert disconnected_http == [503]
        async with http.get("/health/ready") as response:
            assert response.status == 200
        assert connection.transport is not transport
        await fleet.set_bot(active=False)
        stop = fleet.command("STOP_BOT")
        async with build_guarded_broker(fleet.settings) as publisher:
            await publisher.publish(stop.model_dump(mode="json"), exchange=rabbit, mandatory=True)

        async def stopped() -> bool:
            return not telegram.open and not await fleet.redis.exists(fleet.lease_key)

        await eventually(stopped)
        assert await fleet.snapshot() == ("stopped", "bot_inactive", None)


@pytest.mark.parametrize("flush_interval", [1, 300], ids=["normal", "longer-than-drain"])
async def test_sigterm_drains_eager_activity_logger(
    fleet: Fleet,
    telegram: Telegram,
    rabbit: RabbitExchange,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flush_interval: int,
) -> None:
    ctx = fleet.context()
    cfg = ctx.settings
    cfg.logs.dir = tmp_path
    cfg.logs.buffer_flush_interval_seconds = flush_interval
    cfg.logs.buffer_shutdown_timeout_seconds = 3
    outbox = build_outbox_broker(fleet.engine)
    deps: dict[Any, Any] = {
        Settings: cfg,
        Redis: fleet.redis,
        AsyncEngine: fleet.engine,
        OutboxBroker: outbox,
        async_sessionmaker[AsyncSession]: fleet.db,
    }
    provider = Provider(scope=Scope.APP)
    for dependency in deps:
        provider.from_context(dependency)
    provider.from_context(AsyncSession, scope=Scope.REQUEST)
    container = make_async_container(provider, LoggingProvider(), context=deps)
    ctx = replace(ctx, container=container)
    enqueued = asyncio.Event()
    action = f"worker-drain-{uuid4().hex[:12]}"
    query = select(UserActivity.action_type).where(UserActivity.action_type == action)

    started = asyncio.Event()
    original_start = LogBuffer.start

    async def track_start(buffer: LogBuffer) -> None:
        await original_start(buffer)
        started.set()

    monkeypatch.setattr(LogBuffer, "start", track_start)

    @asynccontextmanager
    async def producer(_: ServiceContext) -> AsyncIterator[None]:
        assert started.is_set()
        await telegram.next()
        await eventually(fleet.running)
        logger = await container.get(ActivityLogger)
        logger.log(ActivityEvent(telegram_bot_id=123456, action_type=action))
        async with fleet.db() as reader:
            assert (await reader.scalars(query)).all() == []
        enqueued.set()
        yield

    killer = sigterm_when(enqueued.is_set)
    try:
        assert (
            await asyncio.wait_for(serve_components(ctx, [bot_worker_component, producer]), 10) == 0
        )
        await killer
        assert not telegram.open and not await fleet.redis.exists(fleet.lease_key)
    finally:
        killer.cancel()
        await asyncio.gather(killer, return_exceptions=True)
        await container.close()
    async with fleet.db() as reader:
        assert (await reader.scalars(query)).all() == [action]


async def test_sigterm_cancels_initial_reconcile_and_releases_resources(
    fleet: Fleet, telegram: Telegram, rabbit: RabbitExchange, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = fleet.context()
    registry = ctx.readiness_registry
    assert registry is not None
    in_startup = asyncio.Event()
    cancelled = asyncio.Event()
    original = BotWorker.reconcile

    async def blocked(worker: BotWorker, *, strict: bool = False) -> None:
        await original(worker, strict=strict)
        await telegram.next()
        assert await fleet.redis.exists(fleet.lease_key)
        assert not registry.readiness[scheduler.BOT_RUNTIME_READINESS].ready
        in_startup.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(BotWorker, "reconcile", blocked)
    killer = sigterm_when(in_startup.is_set)
    try:
        assert await asyncio.wait_for(serve_components(ctx, [bot_worker_component]), 10) == 0
        await killer
    finally:
        killer.cancel()
        await asyncio.gather(killer, return_exceptions=True)
    assert cancelled.is_set() and not telegram.open
    assert not registry.readiness[scheduler.BOT_RUNTIME_READINESS].ready
    assert not await fleet.redis.exists(fleet.lease_key, f"{fleet.lease_key}:generation")
    async with build_guarded_broker(fleet.settings) as publisher:
        with pytest.raises(DeliveryError):
            await publisher.publish({"kind": "invalid"}, exchange=rabbit, mandatory=True)


def test_restart_revision_migration_backfills_existing_bots(scratch_db: tuple[str, Query]) -> None:
    url, query = scratch_db
    previous, revision = "20261118_1220", "20261127_1200"
    run_alembic(url, alembic.upgrade, previous)
    query("""
        WITH shop AS (INSERT INTO tenants (id, name, slug)
                      VALUES (gen_random_uuid(), 'Migration shop', 'migration-shop') RETURNING id)
        INSERT INTO bots (id, tenant_id, telegram_bot_id, encrypted_token, token_hash)
        SELECT gen_random_uuid(), id, 123456, 'opaque-test-value', repeat('0', 64) FROM shop
    """)
    run_alembic(url, alembic.upgrade, revision)
    assert query("SELECT restart_revision, pg_typeof(restart_revision)::text FROM bots") == [
        (0, "bigint")
    ]
    query("UPDATE bots SET restart_revision = 4294967296")
    assert query("SELECT restart_revision FROM bots") == [(4294967296,)]
    run_alembic(url, alembic.downgrade, previous)
    assert query("SELECT telegram_bot_id FROM bots") == [(123456,)]
    run_alembic(url, alembic.upgrade, revision)
    assert query("SELECT restart_revision FROM bots") == [(0,)]
