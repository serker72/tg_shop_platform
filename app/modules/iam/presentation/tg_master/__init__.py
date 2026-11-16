"""Master Bot dialogs of the IAM module: shops, staff, moderators (task 05),
roles, personal overrides and effective permissions (task 06).

Handlers and getters hold no business logic: every mutation goes through the admin
services of task 01 (they check permissions, write the audit and commit); reads go through
:class:`IamAdminQueries` and never leave pending changes in the request session.
"""

from app.modules.iam.presentation.tg_master.effective import effective_dialog
from app.modules.iam.presentation.tg_master.overrides import overrides_dialog
from app.modules.iam.presentation.tg_master.roles import roles_dialog
from app.modules.iam.presentation.tg_master.staff import staff_dialog
from app.modules.iam.presentation.tg_master.states import (
    EffectiveSG,
    OverridesSG,
    RolesSG,
    StaffSG,
    TenantsSG,
)
from app.modules.iam.presentation.tg_master.tenants import tenants_dialog

__all__ = [
    "EffectiveSG",
    "OverridesSG",
    "RolesSG",
    "StaffSG",
    "TenantsSG",
    "effective_dialog",
    "overrides_dialog",
    "roles_dialog",
    "staff_dialog",
    "tenants_dialog",
]
