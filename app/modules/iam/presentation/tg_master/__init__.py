"""Master Bot dialogs of the IAM module: shops, staff, moderators (task 05), roles (task 06).

Handlers and getters hold no business logic: every mutation goes through the admin
services of task 01 (they check permissions, write the audit and commit); reads go through
:class:`IamAdminQueries` and never leave pending changes in the request session.
"""

from app.modules.iam.presentation.tg_master.roles import roles_dialog
from app.modules.iam.presentation.tg_master.staff import staff_dialog
from app.modules.iam.presentation.tg_master.states import RolesSG, StaffSG, TenantsSG
from app.modules.iam.presentation.tg_master.tenants import tenants_dialog

__all__ = [
    "RolesSG",
    "StaffSG",
    "TenantsSG",
    "roles_dialog",
    "staff_dialog",
    "tenants_dialog",
]
