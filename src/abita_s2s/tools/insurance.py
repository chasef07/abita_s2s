"""Model-facing insurance tools."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.eligibility_contract import EligibilityInput
from abita_s2s.insurance import InsuranceRegistration, staff
from abita_s2s.state import CallState


class InsuranceTools:
    def __init__(self, insurance: InsuranceRegistration | None) -> None:
        self._insurance = insurance

    @function_tool
    async def check_new_patient_eligibility(
        self,
        context: RunContext[CallState],
        firstName: str,
        lastName: str,
        dob: str,
        plan: str,
        insuranceMemberId: str,
    ) -> str:
        """Start a background eligibility check only for caller-declared new patients.

        Use the patient's own name and DOB (MM/DD/YYYY), plan and card member ID
        as soon as collected, before add_patient. Never use for existing patients
        or self-pay. Continue intake without waiting or announcing coverage.
        Call again only when the submitted details are corrected.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
            return "unavailable: Continue intake; eligibility was not checked."
        if not all(
            v.strip() for v in (firstName, lastName, dob, plan, insuranceMemberId)
        ):
            return "needs_input: Collect name, date of birth, plan and member ID."
        return self._insurance.start_eligibility(
            EligibilityInput(
                firstName=firstName,
                lastName=lastName,
                dob=dob,
                plan=plan,
                memberId=insuranceMemberId,
            )
        )

    @function_tool
    async def check_insurance(
        self,
        context: RunContext[CallState],
        plan: str,
        coverageType: Literal["medical", "routine_vision"],
    ) -> str:
        """Check office participation for the caller's plan and triaged visit type.

        Use before registration or a requested insurance change. Follow clarification
        or staff-review instructions; acceptance does not establish active benefits.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
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
        if self._insurance is None or self._insurance.state is not context.userdata:
            return staff()["answer"]
        result = await self._insurance.update(
            insuranceMemberId, call_id=context.function_call.call_id
        )
        return f"{result['outcome']}: {result['answer']}"
