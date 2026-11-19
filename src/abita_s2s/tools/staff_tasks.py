"""Model-facing staff task tools."""

from livekit.agents import RunContext, function_tool

from abita_s2s.records import Category, Urgency
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.state import CallState


class StaffTaskTools:
    def __init__(self, staff_tasks: StaffTasks) -> None:
        self._staff_tasks = staff_tasks

    @function_tool
    async def save_staff_task(
        self,
        context: RunContext[CallState],
        category: Category,
        urgency: Urgency,
        summary: str,
        message: str,
        draft_id: str | None = None,
        cancel: bool = False,
    ) -> str:
        """Save or update one caller-approved, non-clinical need staff must act on after the call.

        Drafts are sent when the call ends, even on a hangup; say the request is
        noted, not submitted. For records, follow the office knowledge for intake
        and delivery, and say any restrictions or missing prerequisites.

        Args:
            category: Route by Staff task drafts; if unclear, ask one focused question
                before using other.
            urgency: high_priority for time-sensitive work, normal for standard
                follow-up, non_urgent without time sensitivity. Transfer clinical acuity.
            summary: Short staff inbox title for one unresolved need.
            message: Details of exactly one need. Include medication/pharmacy, service/plan,
                authorization status and procedure timing when relevant. Preserve intake
                and list missing details. When updating, include the full revised request.
            draft_id: The ID from this need's previous save, for added details or
                corrections; omit only for a genuinely separate need, even in one category.
            cancel: True with draft_id to withdraw a resolved or unwanted request;
                other fields are ignored.
        """
        context.disallow_interruptions()
        result = self._staff_tasks.save(
            category,
            urgency,
            summary,
            message,
            draft_id=draft_id,
            cancel=cancel,
            call_id=context.function_call.call_id,
        )
        answer = f"{result['outcome']}: {result['answer']}"
        if "draftId" in result:
            answer += f"\nDraft ID: {result['draftId']}"
        if "summary" in result:
            answer += f"\nRequest: {result['summary']}"
            patient = result["patient"]
            answer += (
                f"\nPatient: {patient['name']} ({'verified' if patient['verified'] else 'unverified'})."
                if patient
                else "\nPatient: not identified."
            )
        return answer
