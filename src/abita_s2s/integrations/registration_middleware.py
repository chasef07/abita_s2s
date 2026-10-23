"""One attempt per chart/insurance write; ambiguous responses are never retried."""

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter

from abita_s2s.insurance_contract import CoverageType, InsuranceDecision
from abita_s2s.integrations.middleware import Middleware, OfficeKey
from abita_s2s.integrations.transport import Fault, Reply, Request
from abita_s2s.offices import same_office
from abita_s2s.records import Record, Text


class CreationReceipt(Record):
    insuranceDecision: InsuranceDecision | None = None
    status: Literal["created", "partial"]
    patientId: Text
    name: Text
    dob: Text


class UpdatedReceipt(Record):
    insuranceDecision: InsuranceDecision | None = None
    status: Literal["updated"]
    effect: Literal["completed"]
    patientId: Text
    newInsurance: Text


MutationOutcome = Literal[
    "rejected",
    "reconciled_failure",
    "indeterminate_write",
    "validation_failed",
    "unavailable",
    "failed",
    "reconciled_success",
]
NOT_WRITTEN: tuple[MutationOutcome, ...] = (
    "validation_failed",
    "rejected",
    "reconciled_failure",
    "unavailable",
    "failed",
)


class Rejection(Record):
    status: Literal["error"]
    outcome: Annotated[
        MutationOutcome | None, BeforeValidator(lambda value: value or None)
    ] = None
    message: str = ""

    @property
    def reason(self) -> str:
        return self.message or self.outcome or "backend_failure"


class UpdateRejection(Rejection):
    effect: Literal["no_effect", "partial", "uncertain"]


class WriteFailure(Record):
    status: Literal["failed", "partial", "uncertain"]
    reason: str


CREATED = TypeAdapter(
    Annotated[CreationReceipt | Rejection, Field(discriminator="status")]
)
UPDATED = TypeAdapter(
    Annotated[UpdatedReceipt | UpdateRejection, Field(discriminator="status")]
)
DECISION = TypeAdapter(InsuranceDecision)


class DecisionRequest(Request):
    office: OfficeKey
    plan: str
    coverageType: CoverageType
    dob: str
    offeredPlanIds: list[str] | None = None


class NewPatient(Request):
    firstName: str
    lastName: str
    dob: str
    street: str
    city: str
    state: str
    zip: str
    sex: Literal["male", "female"]
    phone: str
    aptSuite: str
    insurance: str
    subscriberName: str
    subscriberNum: str
    insurancePlanId: str | None = None
    email: str | None = None
    coverageType: CoverageType | None = None
    office: OfficeKey


class InsuranceChange(Request):
    patientId: str
    dob: str
    insurance: str
    coverageType: CoverageType
    subscriberNum: str
    insurancePlanId: str | None = None
    office: OfficeKey


class RegistrationMiddleware(Middleware):
    async def check(self, request: DecisionRequest) -> InsuranceDecision | None:
        reply = await self._post("/api/insurance/decision", request, DECISION)
        if isinstance(reply, str) or not reply.ok or reply.body is None:
            return None
        decision = reply.body
        if (
            not same_office(decision.officeId, request.office)
            or decision.coverageType != request.coverageType
        ):
            return None
        return decision

    async def create(self, request: NewPatient) -> CreationReceipt | WriteFailure:
        reply = await self._post("/api/add-patient", request, CREATED)
        return self._receipt(request, reply)

    async def update(self, request: InsuranceChange) -> UpdatedReceipt | WriteFailure:
        reply = await self._post("/api/patient/update-insurance", request, UPDATED)
        return self._receipt(request, reply)

    @staticmethod
    def _receipt[R: CreationReceipt | UpdatedReceipt](
        request: NewPatient | InsuranceChange,
        reply: Reply[R | Rejection] | Fault,
    ) -> R | WriteFailure:
        if reply == "not_configured":
            return WriteFailure(status="failed", reason="not_configured")
        if isinstance(reply, str) or reply.body is None:
            return WriteFailure(status="uncertain", reason="unverified_write")
        body = reply.body
        if isinstance(body, UpdateRejection):
            return WriteFailure(
                status="failed" if body.effect == "no_effect" else body.effect,
                reason=body.reason,
            )
        if not reply.ok:
            return WriteFailure(status="uncertain", reason="http_failure")
        if isinstance(body, Rejection):
            return WriteFailure(
                status="failed" if body.outcome in NOT_WRITTEN else "uncertain",
                reason=body.reason,
            )
        decision = body.insuranceDecision
        if decision is not None and (
            not same_office(decision.officeId, request.office)
            or decision.coverageType != (request.coverageType or "medical")
            or decision.canonicalPlan != request.insurance
            or (
                decision.planId
                and request.insurancePlanId is not None
                and decision.planId != request.insurancePlanId
            )
        ):
            return WriteFailure(
                status="uncertain", reason="mismatched_insurance_decision"
            )
        return body
