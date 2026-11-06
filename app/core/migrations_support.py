"""Pure helpers for ``migrations/env.py`` (Alembic environment).

``env.py`` runs migrations as an import side effect and is excluded from mypy, so the
logic that deserves tests lives here: the list of model modules, the model importer and
the autogenerate filters (``include_name`` / ``include_object``).

Autogenerate filters
--------------------
pg_partman keeps its catalog in the ``partman`` schema, detached audit partitions are
moved to ``archive``, and every partitioned parent listed in :data:`PARTITIONED_PARENTS`
gets child tables created by pg_partman 5.x. Since 5.0 time-based suffixes are
``_pYYYYMMDD`` for intervals >= 1 day and ``_pYYYYMMDD_HH24MISS`` for shorter intervals,
plus the ``_default`` partition (verified on pg_partman 5.5.0: ``user_activity_p20260701``,
``h_p20261106_020000``, ``audit_logs_default``). None of those children are declared in
``Base.metadata``; without the filters Alembic would emit ``DROP TABLE`` for each of them.

Filtering happens twice: :func:`include_name` drops partitions *before* reflection (cheap,
Alembic calls it for every table name), :func:`include_object` is a safety net for
reflected objects. Only names of the known parents match, so an ordinary table such as
``foo_default`` is never filtered, and the parents themselves stay managed.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections.abc import Iterable, Mapping

from sqlalchemy import Table
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.schema import SchemaItem

from app.core.logging import get_logger

__all__ = [
    "EXCLUDED_SCHEMAS",
    "MANAGED_SCHEMAS",
    "MODEL_MODULES",
    "PARTITIONED_PARENTS",
    "PARTITION_NAME_RE",
    "import_all_models",
    "include_name",
    "include_object",
    "is_importable",
]

logger = get_logger(__name__)

#: Modules that must be imported to populate ``Base.metadata``. One entry per business
#: module; keep it in sync when a module gains ``models.py``.
MODEL_MODULES: tuple[str, ...] = (
    "app.modules.iam.models",
    "app.modules.bot_manager.models",
    "app.modules.catalog.models",
    "app.modules.orders.models",
    "app.modules.billing.models",
    "app.modules.marketing.models",
    "app.modules.support.models",
    "app.modules.logging.models",
    "app.modules.system.models",
)

#: Schemas autogenerate is allowed to inspect. ``None`` is the connection default schema.
#: Extend this set when a module starts owning a dedicated schema.
MANAGED_SCHEMAS: frozenset[str | None] = frozenset({None, "public"})

#: Service schemas owned by pg_partman / partition archiving, never touched by autogenerate.
EXCLUDED_SCHEMAS: frozenset[str] = frozenset({"partman", "archive"})

#: Tables partitioned by pg_partman. Extend this set whenever a new partitioned table is
#: introduced, otherwise its child partitions will show up in autogenerate as DROP TABLE.
PARTITIONED_PARENTS: frozenset[str] = frozenset({"user_activity", "audit_logs"})

#: Child partitions of :data:`PARTITIONED_PARENTS` (pg_partman 5.x naming).
PARTITION_NAME_RE = re.compile(
    r"^(?P<parent>"
    + "|".join(re.escape(parent) for parent in sorted(PARTITIONED_PARENTS))
    + r")_(?:p\d{8}(?:_\d{6})?|default)$"
)


def is_partition_name(name: str | None) -> bool:
    """Return ``True`` for a pg_partman child partition of a known parent table."""
    return name is not None and PARTITION_NAME_RE.match(name) is not None


def include_name(name: str | None, type_: str, parent_names: Mapping[str, str | None]) -> bool:
    """Alembic ``include_name`` hook, evaluated before reflection.

    * schemas: only those listed in :data:`MANAGED_SCHEMAS`;
    * tables: pg_partman child partitions are skipped (never reflected).
    """
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    if type_ == "table":
        return not is_partition_name(name)
    return True


def _table_of(obj: SchemaItem) -> Table | None:
    if isinstance(obj, Table):
        return obj
    try:
        table = getattr(obj, "table", None)
    except InvalidRequestError:  # detached Constraint: ``.table`` raises
        return None
    return table if isinstance(table, Table) else None


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    """Alembic ``include_object`` hook.

    Safety net behind :func:`include_name`:

    * everything inside :data:`EXCLUDED_SCHEMAS` is skipped;
    * a reflected partition table not declared in ``target_metadata``
      (``reflected and compare_to is None``) is skipped;
    * columns / indexes / constraints whose table is a partition are skipped (partition
      children only ever come from reflection).

    Parent tables (``user_activity``, ``audit_logs``) and their children are kept.
    """
    table = _table_of(obj)
    if table is not None and table.schema in EXCLUDED_SCHEMAS:
        return False
    if type_ == "table":
        return not (reflected and compare_to is None and is_partition_name(name))
    return not (table is not None and is_partition_name(table.name))


def is_importable(module_name: str) -> bool:
    """Return ``True`` if ``module_name`` can be resolved.

    Only a missing ``module_name`` itself (or one of its parent packages) counts as
    "not importable". A ``ModuleNotFoundError`` for any *other* module - e.g. a broken
    import inside an existing package ``__init__`` - is propagated.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            logger.debug("model_module_skipped", module=module_name, missing=missing)
            return False
        raise
    if spec is None:
        logger.debug("model_module_skipped", module=module_name, missing=module_name)
        return False
    return True


def import_all_models(module_names: Iterable[str] = MODEL_MODULES) -> list[str]:
    """Import every existing model module and return the names actually imported.

    Missing modules are skipped (no business models yet); a module that exists but raises
    on import propagates its exception so a broken model never goes unnoticed.
    """
    imported: list[str] = []
    for module_name in module_names:
        if not is_importable(module_name):
            continue
        importlib.import_module(module_name)
        imported.append(module_name)
    return imported
