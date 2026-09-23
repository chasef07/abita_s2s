"""Intake-scoped eligibility evidence; never chart identity or booking permission."""

from dataclasses import dataclass
from typing import Literal

from abita_s2s.integrations.patient_middleware import Record, Text


class EligibilityInput(Record):
    firstName: Text
    lastName: Text
    dob: Text
    memberId: Text
    plan: Text


class IdentityEvidence(Record):
    status: Text
    reviewRequired: bool
    reasons: list[str] | None = None


class EligibilityResult(Record):
    status: Literal["active", "inactive", "review", "unknown"]
    officeId: Text
    checkedAt: Text
    payerId: str | None = None
    reviewReason: str | None = None
    eligibilitySearchId: str | None = None
    checkId: str | None = None
    errorCodes: list[str] | None = None
    identity: IdentityEvidence | None = None


@dataclass(repr=False)
class EligibilityCheck:
    office: str
    request: EligibilityInput
    status: Literal["pending", "complete", "unavailable"] = "pending"
    result: EligibilityResult | None = None
    failure_reason: str | None = None
