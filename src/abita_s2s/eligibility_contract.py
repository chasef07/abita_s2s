"""Intake-scoped eligibility evidence; never chart identity or booking permission."""

from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict, JsonValue

from abita_s2s.integrations.patient_middleware import Record, Text


class EligibilityInput(Record):
    firstName: Text
    lastName: Text
    dob: Text
    memberId: Text
    plan: Text


class IdentityEvidence(Record):
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    status: Text
    reviewRequired: bool
    reasons: list[str] | None = None


class EligibilityResult(Record):
    # Preserve future middleware fields as well as opaque payer evidence.
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    providerResponse: JsonValue = None
    providerHttpStatus: int | None = None
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
