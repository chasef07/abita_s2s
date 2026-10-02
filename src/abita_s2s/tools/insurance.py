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
    async def check_insurance(
        self,
        context: RunContext[CallState],
        plan: str,
        coverageType: CoverageType,
    ) -> str:
        """Check office participation for the caller's insurance and triaged visit type.

        Pass whatever the caller said about their insurance, word for word: a name,
        part of a name, or what is on the card. If the result asks which plan, read
        the options and call again with the one the caller picks. Use before
        registration or a requested insurance change. Follow staff-review
        instructions; acceptance does not establish active benefits.
        """
        if not bound(self._insurance, context):
            return staff()["answer"]
        return (await self._insurance.check(plan, coverageType))["answer"]

    @function_tool
    async def update_insurance(
        self,
        context: RunContext[CallState],
        insuranceMemberId: str,
    ) -> str:
        """Change the active verified patient's coverage only when the caller requests it.

        First use check_insurance for the new plan and correct visit type. Supply the
        card member ID, or self pay after Self Pay is accepted. Use add_patient for
        registration. Claim success only from an updated receipt; never retry an
        uncertain result or repeat a completed write.
        """
        if not bound(self._insurance, context):
            return staff()["answer"]
        result = await self._insurance.update(
            insuranceMemberId, call_id=context.function_call.call_id
        )
        return f"{result['outcome']}: {result['answer']}"
