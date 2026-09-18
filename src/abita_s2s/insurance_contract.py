"""Backend participation decisions; no local plan matching or coverage rules."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InsuranceRequirement(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    kind: str
    channel: str = ""
    verification: Literal["unverified"]


class InsuranceDecision(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    outcome: Literal["accepted", "not_accepted", "needs_clarification", "needs_staff_task"]
    participation: Literal["accepted", "not_accepted", "unknown"]
    canonicalPlan: str = ""
    carrierCode: str = ""
    coverageType: Literal["medical", "routine_vision"]
    officeId: str
    routing: str = ""
    credentialedProviders: list[str] = Field(default_factory=list)
    allowedProviders: list[str]
    requirements: list[InsuranceRequirement]
    eligibility: Literal["not_checked"]
    canSchedule: bool
    selfPay: bool
    answer: str

    @model_validator(mode="after")
    def coherent(self):
        if self.participation == "accepted" and not self.canonicalPlan:
            raise ValueError("Missing accepted plan")
        if self.canSchedule and self.participation != "accepted":
            raise ValueError("Scheduling requires an accepted plan")
        if self.canSchedule and (self.requirements or not self.allowedProviders):
            raise ValueError("Scheduling requires verified backend policy")
        return self
