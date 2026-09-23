"""Intake-scoped eligibility evidence; never chart identity or booking permission."""

import asyncio
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, JsonValue

from abita_s2s.integrations.patient_middleware import Record, Text


class EligibilityInput(Record):
    firstName: Text
    lastName: Text
    dob: Text
    memberId: Text
    plan: Text
    coverageType: Literal["medical", "routine_vision"] = "medical"


class IdentityEvidence(Record):
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    status: Text
    reviewRequired: bool
    reasons: list[str] | None = None


class MatchedPatient(Record):
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    firstName: Text
    lastName: Text
    dateOfBirth: Text
    memberId: str | None = None


class EligibilityProvider(Record):
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    profileId: Text
    name: Text
    firstName: Text
    lastName: Text
    npi: Text


class EligibilityResult(Record):
    # Preserve future middleware fields as well as opaque payer evidence.
    model_config = ConfigDict(strict=True, frozen=True, extra="allow")
    provider: EligibilityProvider | None = None
    providerResults: list["EligibilityResult"] | None = None
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
    matchedPatient: MatchedPatient | None = None

    @property
    def name_correction(self) -> MatchedPatient | None:
        if (
            self.identity
            and self.identity.status == "matched_with_name_correction"
            and not self.identity.reviewRequired
        ):
            return self.matchedPatient
        return None


@dataclass(repr=False)
class EligibilityCheck:
    office: str
    request: EligibilityInput
    id: str = field(default_factory=lambda: str(uuid4()))
    patient_id: str | None = None
    canonical_plan: str | None = None
    invalidated: bool = False
    status: Literal["pending", "complete", "unavailable"] = "pending"
    result: EligibilityResult | None = None
    failure_reason: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
