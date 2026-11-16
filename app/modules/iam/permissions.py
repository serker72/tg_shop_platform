"""Seed catalogue of IAM permissions and system roles (dot-notation slugs).

Permission slugs follow ``{module}.{action}`` (TZ 46.7 names lower-cased, the first
``_`` turned into ``.``). The ``iam_rbac`` revision freezes the very same rows as literals;
``tests`` verify that the migration seed equals these constants.

``users`` are staff only (shop customers live in ``customers`` and have no permissions):
``users.view`` opens the "Staff" section, ``users.manage`` adds / (de)activates / deletes /
restores staff (``admin`` only), ``logs.view`` opens the "Logs" section (``admin`` only).

Roles are global. ``admin`` holds every permission. ``senior_moderator`` and
``moderator`` have the same permissions except ``tenants.view_all``: the senior works
with all shops, the moderator only with the shops assigned in ``tenant_moderators``
(``moderator`` is derived as ``senior_moderator - {tenants.view_all}``).

"All permissions" of ``admin`` is fixed only by the seed: every permission added by a
future migration must be granted to ``admin`` explicitly in that migration.
"""

from __future__ import annotations

__all__ = [
    "ACCOUNTING_MANAGE",
    "ACCOUNTING_VIEW",
    "CASES_CLOSE",
    "CASES_REFUND",
    "CASES_REPLACE",
    "CASES_REPLY",
    "CASES_VIEW",
    "GLOBAL_PERMISSIONS",
    "IAM_RBAC_PERMISSIONS",
    "LATER_PERMISSIONS",
    "LOGS_VIEW",
    "MODERATORS_MANAGE",
    "ORDERS_MANAGE",
    "ORDERS_VIEW",
    "PAYOUTS_APPROVE",
    "PAYOUTS_REQUEST",
    "PAYOUTS_VIEW",
    "PREORDERS_MANAGE",
    "PREORDERS_VIEW",
    "PROXIES_MANAGE",
    "PRODUCTS_CREATE",
    "PRODUCTS_DELETE",
    "PRODUCTS_EDIT",
    "PRODUCTS_VIEW",
    "REFERRALS_VIEW",
    "REVIEWS_MANAGE",
    "REVIEWS_VIEW",
    "ROLES_MANAGE",
    "ROLE_ADMIN",
    "ROLE_MODERATOR",
    "ROLE_SENIOR_MODERATOR",
    "SEED_PERMISSIONS",
    "SEED_PERMISSION_SLUGS",
    "SEED_ROLES",
    "SEED_ROLE_PERMISSIONS",
    "SETTINGS_MANAGE",
    "STATISTICS_VIEW",
    "TENANTS_VIEW_ALL",
    "USERS_MANAGE",
    "USERS_VIEW",
]

PRODUCTS_VIEW = "products.view"
PRODUCTS_CREATE = "products.create"
PRODUCTS_EDIT = "products.edit"
PRODUCTS_DELETE = "products.delete"
ORDERS_VIEW = "orders.view"
ORDERS_MANAGE = "orders.manage"
CASES_VIEW = "cases.view"
CASES_REPLY = "cases.reply"
CASES_REFUND = "cases.refund"
CASES_REPLACE = "cases.replace"
CASES_CLOSE = "cases.close"
USERS_VIEW = "users.view"
USERS_MANAGE = "users.manage"
REFERRALS_VIEW = "referrals.view"
REVIEWS_VIEW = "reviews.view"
REVIEWS_MANAGE = "reviews.manage"
PREORDERS_VIEW = "preorders.view"
PREORDERS_MANAGE = "preorders.manage"
PAYOUTS_VIEW = "payouts.view"
PAYOUTS_REQUEST = "payouts.request"
PAYOUTS_APPROVE = "payouts.approve"
ACCOUNTING_VIEW = "accounting.view"
ACCOUNTING_MANAGE = "accounting.manage"
STATISTICS_VIEW = "statistics.view"
MODERATORS_MANAGE = "moderators.manage"
ROLES_MANAGE = "roles.manage"
SETTINGS_MANAGE = "settings.manage"
#: Access to every shop without an assignment in ``tenant_moderators``.
TENANTS_VIEW_ALL = "tenants.view_all"
#: Proxy pool of the bots (add / ban / activate); added by the ``proxy_pool`` revision.
PROXIES_MANAGE = "proxies.manage"
#: "Logs" section of the Master Bot (click log / audit); added by ``staff_permissions``.
LOGS_VIEW = "logs.view"

#: ``(slug, name)`` rows seeded by the ``iam_rbac`` revision.
IAM_RBAC_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (PRODUCTS_VIEW, "Просмотр товаров"),
    (PRODUCTS_CREATE, "Создание товаров"),
    (PRODUCTS_EDIT, "Редактирование товаров"),
    (PRODUCTS_DELETE, "Удаление товаров"),
    (ORDERS_VIEW, "Просмотр заказов"),
    (ORDERS_MANAGE, "Управление заказами"),
    (CASES_VIEW, "Просмотр обращений"),
    (CASES_REPLY, "Ответ на обращения"),
    (CASES_REFUND, "Возврат по обращению"),
    (CASES_REPLACE, "Замена по обращению"),
    (CASES_CLOSE, "Закрытие обращений"),
    (USERS_VIEW, "Просмотр пользователей"),
    (USERS_MANAGE, "Управление пользователями"),
    (REFERRALS_VIEW, "Просмотр рефералов"),
    (REVIEWS_VIEW, "Просмотр отзывов"),
    (REVIEWS_MANAGE, "Управление отзывами"),
    (PREORDERS_VIEW, "Просмотр предзаказов"),
    (PREORDERS_MANAGE, "Управление предзаказами"),
    (PAYOUTS_VIEW, "Просмотр выплат"),
    (PAYOUTS_REQUEST, "Запрос выплат"),
    (PAYOUTS_APPROVE, "Одобрение выплат"),
    (ACCOUNTING_VIEW, "Просмотр бухгалтерии"),
    (ACCOUNTING_MANAGE, "Управление бухгалтерией"),
    (STATISTICS_VIEW, "Просмотр статистики"),
    (MODERATORS_MANAGE, "Управление модераторами"),
    (ROLES_MANAGE, "Управление ролями"),
    (SETTINGS_MANAGE, "Управление настройками"),
    (TENANTS_VIEW_ALL, "Доступ ко всем магазинам"),
)
#: ``(slug, name)`` rows added by later revisions (each one also grants them to ``admin``).
LATER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (PROXIES_MANAGE, "Управление прокси"),  # revision proxy_pool
    (LOGS_VIEW, "Просмотр логов"),  # revision staff_permissions
)
#: The whole permission catalogue at ``head``.
SEED_PERMISSIONS: tuple[tuple[str, str], ...] = IAM_RBAC_PERMISSIONS + LATER_PERMISSIONS
SEED_PERMISSION_SLUGS: frozenset[str] = frozenset(slug for slug, _ in SEED_PERMISSIONS)

#: Platform-wide permissions that make no sense per shop: a tenant-scoped override
#: (``user_permission_overrides.tenant_id IS NOT NULL``) of these is forbidden by the service.
GLOBAL_PERMISSIONS: frozenset[str] = frozenset(
    {
        ROLES_MANAGE,
        SETTINGS_MANAGE,
        TENANTS_VIEW_ALL,
        MODERATORS_MANAGE,
        PROXIES_MANAGE,
        # Staff and logs sections are not bound to a shop.
        USERS_VIEW,
        USERS_MANAGE,
        LOGS_VIEW,
    }
)

ROLE_ADMIN = "admin"
ROLE_SENIOR_MODERATOR = "senior_moderator"
ROLE_MODERATOR = "moderator"

#: ``(slug, name)`` of the system roles (``is_system = true``) seeded by ``iam_rbac``.
SEED_ROLES: tuple[tuple[str, str], ...] = (
    (ROLE_ADMIN, "Администратор"),
    (ROLE_SENIOR_MODERATOR, "Старший модератор"),
    (ROLE_MODERATOR, "Модератор"),
)

_SENIOR_MODERATOR_PERMISSIONS: frozenset[str] = frozenset(
    {
        TENANTS_VIEW_ALL,
        PRODUCTS_VIEW,
        ORDERS_VIEW,
        CASES_VIEW,
        USERS_VIEW,
        REFERRALS_VIEW,
        REVIEWS_VIEW,
        PREORDERS_VIEW,
        PAYOUTS_VIEW,
        STATISTICS_VIEW,
        PRODUCTS_CREATE,
        PRODUCTS_EDIT,
        PRODUCTS_DELETE,
        ORDERS_MANAGE,
        CASES_REPLY,
        CASES_REFUND,
        CASES_REPLACE,
        CASES_CLOSE,
        REVIEWS_MANAGE,
        PREORDERS_MANAGE,
        MODERATORS_MANAGE,
    }
)

#: Permissions of each seeded system role.
SEED_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: SEED_PERMISSION_SLUGS,
    ROLE_SENIOR_MODERATOR: _SENIOR_MODERATOR_PERMISSIONS,
    ROLE_MODERATOR: _SENIOR_MODERATOR_PERMISSIONS - {TENANTS_VIEW_ALL},
}
