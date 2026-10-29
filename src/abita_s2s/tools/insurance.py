"""Model-facing insurance tools."""

from livekit.agents import RunContext, function_tool

from abita_s2s.insurance_contract import CoverageType
from abita_s2s.insurance import InsuranceRegistration, staff
from abita_s2s.state import CallState
from abita_s2s.tools.context import bound


class InsuranceTools:
    def __init__(self, insurance: InsuranceRegistration | None) -> None:
        self._insurance = insurance

    @function_tool
    async def update_insurance(
        self,
        context: RunContext[CallState],
        insurancePlanId: str,
        visitType: CoverageType,
        insuranceMemberId: str,
    ) -> str:
        """Change the active verified patient's coverage only when the caller requests it.

        Send the new plan's ID from the Insurance list for the visit type and the card
        member ID; a self-pay plan needs no member ID. Use add_patient for
        registration. Claim success only from an updated receipt; never retry an
        uncertain result or repeat a completed write.
        """
        if not bound(self._insurance, context):
            return staff()["answer"]
        result = await self._insurance.update(
            insurancePlanId,
            visitType,
            insuranceMemberId,
            call_id=context.function_call.call_id,
        )
        return result["answer"]
