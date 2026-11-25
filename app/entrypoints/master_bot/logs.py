"""Master Bot composition for the logging dialog's staff timezone lookup."""

from dishka import Provider, Scope, provide

from app.core.config import Settings
from app.modules.iam.presentation.tg_master.queries import IamAdminQueries
from app.modules.logging.presentation.tg_master.logs import StaffTimezoneReader
from app.shared_kernel.timezones import effective_staff_timezone


class MasterBotLogsProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def staff_timezone(self, queries: IamAdminQueries, settings: Settings) -> StaffTimezoneReader:
        async def read(actor: int) -> str:
            user = await queries.user(actor)
            return effective_staff_timezone(
                user.timezone if user is not None else None, settings.default_timezone
            )

        return StaffTimezoneReader(read)
