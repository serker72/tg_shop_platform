"""Effective permissions of a staff member (task 06): why exactly a permission is held.

Started from the staff card with ``start_data={"user_id": <telegram id>}``. Reading needs
the global ``users.view`` (the same right as the staff card), re-checked in every getter
and handler: ``when`` hides buttons only at render time. The data comes from
:meth:`PermissionService.effective_permissions` (PostgreSQL, the same ``decide`` rule as a
live check). Shops are limited to the viewer's scope (:func:`load_tenant`).

A long report is split into pages by lines so that every message stays below the
4096 characters of Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime
from operator import itemgetter
from typing import Any, Final
from uuid import UUID

from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.kbd import (
    Button,
    Cancel,
    Group,
    ScrollingGroup,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.text import Format

from app.core.i18n import gettext as _
from app.modules.iam.admin_service import TenantAdminService
from app.modules.iam.effective_permissions import (
    EffectivePermission,
    EffectivePermissionsReport,
    OverrideRef,
    UserStatus,
)
from app.modules.iam.presentation.tg_master.common import (
    can_view_staff,
    load_tenant,
    snapshot_of,
)
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.iam.presentation.tg_master.roles import PERMISSION_NAMES
from app.modules.iam.presentation.tg_master.staff import parse_telegram_id
from app.modules.iam.presentation.tg_master.states import EffectiveSG
from app.modules.iam.service import PermissionService
from app.modules.iam.snapshot import DecisionSource
from app.shared_kernel.presentation.tg_master.common import (
    actor_id,
    compose,
    denied_text,
    error_text,
    nav_texts,
    resolve,
    set_notice,
    short,
)

__all__ = ["PAGE_LIMIT", "effective_dialog", "paginate"]

#: Body characters per page; header, page counter and notice fit in the rest of 4096.
PAGE_LIMIT: Final = 3300
SHOPS_PAGE: Final = 10


def paginate(lines: list[str], limit: int = PAGE_LIMIT) -> list[str]:
    """Join ``lines`` into pages of at most ``limit`` characters (a longer line is cut)."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for raw in lines:
        line = raw if len(raw) <= limit else raw[: limit - 1] + "…"
        if current and size + len(line) + 1 > limit:
            pages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current or not pages:
        pages.append("\n".join(current))
    return pages


# --- access and helpers ----------------------------------------------------------------


def _allowed(manager: DialogManager) -> bool:
    return can_view_staff(snapshot_of(manager))


async def _guard(callback: CallbackQuery, manager: DialogManager) -> bool:
    if _allowed(manager):
        return True
    await callback.answer(denied_text())
    await manager.done()
    return False


def _target(manager: DialogManager) -> int | None:
    start = manager.start_data if isinstance(manager.start_data, dict) else {}
    try:
        return parse_telegram_id(str(start.get("user_id", "")))
    except ValueError:
        return None


def _labels() -> dict[str, Any]:
    return {
        "btn_shops": _("Права в магазине"),
        "btn_general": _("Общие права"),
        "btn_prev": "‹",
        "btn_next": "›",
        **nav_texts(),
    }


def _page(manager: DialogManager, *lines: str, **data: Any) -> dict[str, Any]:
    return {
        "has_prev": False,
        "has_next": False,
        "ok": False,
        "tenants": [],
        **_labels(),
        **data,
        "text": compose(manager, *lines),
    }


def _status(report: EffectivePermissionsReport) -> str:
    texts = {
        UserStatus.UNKNOWN: _("не найден"),
        UserStatus.ACTIVE: _("активен"),
        UserStatus.INACTIVE: _("отключён — прав нет"),
        UserStatus.DELETED: _("удалён — прав нет"),
    }
    return texts.get(report.status, str(report.status))


def _expiry(expires_at: datetime | None) -> str:
    if expires_at is None:
        return _("бессрочно")
    return _("до {when}").format(when=expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))


class _Describer:
    """Texts of report entries; granting staff members are read once per render."""

    def __init__(self, manager: DialogManager, queries: IamAdminQueries) -> None:
        self._manager = manager
        self._queries = queries
        self._granters: dict[UUID, str] = {}
        self._shops: dict[UUID, str | None] = {}

    async def granter(self, user_id: UUID | None) -> str:
        if user_id is None:
            return "—"
        if user_id not in self._granters:
            brief = await self._queries.user_brief(user_id)
            if brief is None:
                self._granters[user_id] = "—"
            else:
                tg_id, username = brief
                self._granters[user_id] = f"@{username} ({tg_id})" if username else str(tg_id)
        return self._granters[user_id]

    async def shop(self, tenant_id: UUID | None) -> str | None:
        """``all shops`` / shop name in the viewer's scope / ``None`` out of scope."""
        if tenant_id is None:
            return _("все магазины")
        if tenant_id not in self._shops:
            found = await load_tenant(self._manager, tenant_id)
            self._shops[tenant_id] = None if found is None else found[0].name
        return self._shops[tenant_id]

    async def override(self, ref: OverrideRef) -> str:
        effect = _("разрешено") if ref.is_granted else _("запрещено")
        scope = await self.shop(ref.tenant_id) or _("магазин вне вашей области")
        parts = [
            _("персонально: {effect}").format(effect=effect),
            scope,
            _expiry(ref.expires_at),
            _("выдал: {who}").format(who=await self.granter(ref.granted_by_user_id)),
        ]
        if ref.reason:
            parts.append(_("причина: {reason}").format(reason=ref.reason))
        return ", ".join(parts)

    async def source(self, item: EffectivePermission) -> str:
        if item.source is DecisionSource.ROLE:
            names = ", ".join(role.name for role in item.roles) or "—"
            return _("роль: {names}").format(names=names)
        if item.override is not None:
            return await self.override(item.override)
        texts = {
            DecisionSource.SUPERUSER: _("суперпользователь"),
            DecisionSource.NO_TENANT_ACCESS: _("нет доступа к магазину"),
            DecisionSource.NOT_GRANTED: _("не выдано"),
            DecisionSource.UNKNOWN_USER: _("пользователь не найден или отключён"),
        }
        return texts.get(item.source, str(item.source))

    async def entry(self, item: EffectivePermission) -> str:
        name = PERMISSION_NAMES.get(item.permission_slug, item.permission_slug)
        mark = "✅" if item.allowed else "⛔"
        return f"{mark} {name} — {await self.source(item)}"


async def _report(manager: DialogManager) -> EffectivePermissionsReport | None:
    target = _target(manager)
    if target is None:
        return None
    service = await resolve(manager, PermissionService)
    return await service.effective_permissions(target)


def _paged(manager: DialogManager, header: list[str], body: list[str]) -> dict[str, Any]:
    pages = paginate(body)
    page = min(max(int(manager.dialog_data.get("page", 0) or 0), 0), len(pages) - 1)
    manager.dialog_data["page"] = page
    lines = [*header]
    if len(pages) > 1:
        lines.append(_("Страница {page} из {total}").format(page=page + 1, total=len(pages)))
    lines.append(pages[page])
    return _page(manager, *lines, ok=True, has_prev=page > 0, has_next=page < len(pages) - 1)


def _header(report: EffectivePermissionsReport) -> list[str]:
    lines = [
        _("Фактические права сотрудника {id}").format(id=report.telegram_user_id),
        _("Статус: {status}").format(status=_status(report)),
    ]
    if report.roles:
        lines.append(_("Роли: {names}").format(names=", ".join(r.name for r in report.roles)))
    return lines


# --- getters ---------------------------------------------------------------------------


async def general_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    report = await _report(dialog_manager)
    if report is None:
        return _page(dialog_manager, error_text("user_not_found"))
    header = _header(report)
    if report.is_superuser:
        return _page(dialog_manager, *header, _("Все права (суперпользователь)."))
    if not report.is_active:
        return _page(dialog_manager, *header)
    describer = _Describer(dialog_manager, await resolve(dialog_manager, IamAdminQueries))
    body = [_("Общие (все магазины):")]
    body.extend([await describer.entry(e) for e in report.permissions if e.tenant_id is None])
    expired: list[str] = []
    hidden = 0
    for item in report.expired_overrides:
        if item.override is None or await describer.shop(item.tenant_id) is None:
            hidden += 1
            continue
        name = PERMISSION_NAMES.get(item.permission_slug, item.permission_slug)
        expired.append(f"⌛ {name} — {await describer.override(item.override)}")
    body.append("")
    body.append(_("Истёкшие персональные права:"))
    body.extend(expired or ["—"])
    if hidden:
        body.append(_("Ещё в магазинах вне вашей области: {count}").format(count=hidden))
    return _paged(dialog_manager, header, body)


async def tenants_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    service = await resolve(dialog_manager, TenantAdminService)
    tenants = await service.list_tenants(actor_id(dialog_manager))
    return _page(
        dialog_manager,
        _("Выберите магазин."),
        ok=True,
        tenants=[(str(t.id), short(f"{t.name} ({t.slug})")) for t in tenants],
    )


async def shop_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not _allowed(dialog_manager):
        return _page(dialog_manager, denied_text())
    found = await load_tenant(dialog_manager, dialog_manager.dialog_data.get("tenant_id"))
    if found is None or found[0].is_deleted:
        return _page(dialog_manager, error_text("tenant_not_found"))
    tenant = found[0]
    report = await _report(dialog_manager)
    if report is None:
        return _page(dialog_manager, error_text("user_not_found"))
    header = _header(report)
    header.append(_("Магазин: {name} ({slug})").format(name=tenant.name, slug=tenant.slug))
    if report.is_superuser:
        return _page(dialog_manager, *header, _("Все права (суперпользователь)."))
    if not report.is_active:
        return _page(dialog_manager, *header)
    if tenant.id not in report.tenants:
        return _page(
            dialog_manager,
            *header,
            _(
                "Сотрудник не привязан к этому магазину и не имеет в нём персональных прав: "
                "в магазине действуют только общие права при доступе ко всем магазинам."
            ),
        )
    describer = _Describer(dialog_manager, await resolve(dialog_manager, IamAdminQueries))
    body = [await describer.entry(e) for e in report.permissions if e.tenant_id == tenant.id]
    return _paged(dialog_manager, header, body)


# --- handlers --------------------------------------------------------------------------


async def on_prev(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _guard(callback, manager):
        manager.dialog_data["page"] = max(int(manager.dialog_data.get("page", 0) or 0) - 1, 0)


async def on_next(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _guard(callback, manager):
        manager.dialog_data["page"] = int(manager.dialog_data.get("page", 0) or 0) + 1


async def on_general(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _guard(callback, manager):
        manager.dialog_data["page"] = 0
        await manager.switch_to(EffectiveSG.list)


async def on_shop(callback: CallbackQuery, widget: Any, manager: DialogManager, item: str) -> None:
    if not await _guard(callback, manager):
        return
    found = await load_tenant(manager, item)
    if found is None or found[0].is_deleted:
        set_notice(manager, error_text("tenant_not_found"))
        return
    manager.dialog_data["tenant_id"] = str(found[0].id)
    manager.dialog_data["page"] = 0
    await manager.switch_to(EffectiveSG.shop)


# --- dialog ----------------------------------------------------------------------------


def _pager(prefix: str) -> Group:
    return Group(
        Button(Format("{btn_prev}"), id=f"{prefix}_prev", on_click=on_prev, when="has_prev"),
        Button(Format("{btn_next}"), id=f"{prefix}_next", on_click=on_next, when="has_next"),
        width=2,
    )


def effective_dialog() -> Dialog:
    """New :class:`Dialog` instance (a dialog router can be included only once)."""
    text = Format("{text}")
    return Dialog(
        Window(
            text,
            _pager("ef"),
            SwitchTo(Format("{btn_shops}"), id="ef_shops", state=EffectiveSG.tenant, when="ok"),
            Cancel(Format("{btn_back}")),
            state=EffectiveSG.list,
            getter=general_getter,
        ),
        Window(
            text,
            ScrollingGroup(
                Select(
                    Format("{item[1]}"),
                    id="ef_shop",
                    item_id_getter=itemgetter(0),
                    items="tenants",
                    on_click=on_shop,
                ),
                id="ef_shop_scroll",
                width=1,
                height=SHOPS_PAGE,
                hide_on_single_page=True,
            ),
            Button(Format("{btn_general}"), id="ef_t_general", on_click=on_general),
            state=EffectiveSG.tenant,
            getter=tenants_getter,
        ),
        Window(
            text,
            _pager("efs"),
            SwitchTo(Format("{btn_shops}"), id="efs_shops", state=EffectiveSG.tenant),
            Button(Format("{btn_general}"), id="efs_general", on_click=on_general),
            state=EffectiveSG.shop,
            getter=shop_getter,
        ),
    )
