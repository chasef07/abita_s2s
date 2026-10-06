"""Model-facing staff task tools."""

from livekit.agents import RunContext, function_tool

from abita_s2s.staff_tasks import Category, StaffTasks, Urgency
from abita_s2s.state import CallState
from abita_s2s.tools.context import bound


class StaffTaskTools:
    def __init__(self, staff_tasks: StaffTasks | None) -> None:
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
        """Save or update one caller-approved, non-clinical unresolved need for call-end delivery.

        Drafts are sent automatically when the call ends, including a caller hangup.
        Say the request is noted, not submitted; staff owns fulfillment and timing.
        Reuse draft_id for added details or corrections to the same request, including
        pharmacy details. Omit it only for a genuinely separate need, even in one category.
        For records, search office knowledge for intake/delivery rules; speak restrictions
        and missing prerequisites. Collect available details and list remaining gaps.
        Follow the Transfer policy for urgent or clinical concerns.

        Args:
            category: Follow the Staff task drafts routing rules. Classify the work
                requested; ask one focused question if unclear before using other.
            urgency: high_priority for time-sensitive non-clinical work, normal for
                standard follow-up, non_urgent without time sensitivity. Transfer clinical acuity.
            summary: Short staff inbox title for one unresolved need.
            message: Details of exactly one need. Include medication/pharmacy, service/plan,
                authorization status and procedure timing when relevant. Preserve intake
                and list missing details. When updating, include the full revised request.
            draft_id: ID returned by the previous save for this need; omit for a new need.
            cancel: True with the draft ID if the request is withdrawn or resolved;
                it will not be submitted. Other request fields are ignored when cancelling.
        """
        context.disallow_interruptions()
        if not bound(self._staff_tasks, context):
            return "failed: Staff delivery is unavailable. No request was sent."
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
