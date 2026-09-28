"""Model-facing transfer and end-call tools."""

from livekit.agents import RunContext, function_tool
from livekit.agents.beta.tools.end_call import EndCallTool

from abita_s2s.call_control import CallControl
from abita_s2s.state import CallState
from abita_s2s.tools.context import say_only


class CallControlTools(EndCallTool):
    def __init__(self, control: CallControl) -> None:
        super().__init__(
            delete_room=False, end_instructions="Say a brief goodbye to the caller."
        )
        self._control = control

    @function_tool
    async def transfer_call(self, ctx: RunContext[CallState]) -> str:
        """Transfer to human staff when office policy requires it, including emergencies or named staff.

        No patient lookup is required. Call without announcing; this tool speaks first.
        Retry only if the result explicitly offers one retry. Never claim a human answered.
        """
        return await self._control.transfer(
            ctx.disallow_interruptions,
            lambda: say_only(ctx, "One moment while I transfer you to the office."),
        )

    async def aclose(self) -> None:
        self._control.close_admission()
        await super().aclose()

    async def _end_call(self, ctx: RunContext):
        if blocked := self._control.begin_end(ctx.disallow_interruptions):
            return blocked
        return await super()._end_call(ctx)
