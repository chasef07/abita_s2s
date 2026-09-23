"""Model-facing staff task tools."""

from livekit.agents import RunContext, function_tool

from abita_s2s.staff_tasks import Category, StaffTasks, Urgency
from abita_s2s.state import CallState


class StaffTaskTools:
    def __init__(self, staff_tasks: StaffTasks | None) -> None:
        self._staff_tasks = staff_tasks

    @function_tool
    async def create_staff_task(
        self,
        context: RunContext[CallState],
        category: Category,
        urgency: Urgency,
        summary: str,
        message: str,
    ) -> str:
        """Send one safe, non-urgent caller-approved unresolved need per invocation.

        Submit distinct needs separately, even in one category. For records, search
        office knowledge for intake/delivery rules; speak restrictions and missing
        prerequisites even when approved. Collect details and list gaps if incomplete.
        Follow Human Transfer policy for urgent or clinical concerns. Confirm submission
        only after success; staff owns fulfillment and timing.

        Args:
            category: Optical includes glasses/contact prescriptions; medication includes
                refills and medication authorizations; insurance includes copays, coverage,
                referrals requirements and service authorizations; referrals means specialist
                or imaging orders. pre_op/post_op are surgical preparation/aftercare.
                Ask what prior authorization authorizes; if still unclear use other.
            urgency: high_priority for time-sensitive non-clinical work, normal for
                standard follow-up, non_urgent without time sensitivity. Transfer clinical acuity.
            summary: Short staff inbox title for one unresolved need.
            message: Details of exactly one need. Include medication/pharmacy, service/plan,
                authorization status and procedure timing when relevant. Preserve intake
                and list missing details. Make another invocation for each additional need.
        """
        context.disallow_interruptions()
        if self._staff_tasks is None or self._staff_tasks.state is not context.userdata:
            return "failed: Staff delivery is unavailable. No request was sent."
        result = await self._staff_tasks.submit(
            category, urgency, summary, message, call_id=context.function_call.call_id
        )
        answer = f"{result['outcome']}: {result['answer']}"
        if "summary" in result:
            answer += f"\nRequest: {result['summary']}"
            patient = result["patient"]
            answer += (
                f"\nPatient: {patient['name']} ({'verified' if patient['verified'] else 'unverified'})."
                if patient
                else "\nPatient: not identified."
            )
        return answer
