"""Backend participation decisions; no local plan matching or coverage rules."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CoverageType = Literal["medical", "routine_vision"]


class InsuranceRequirement(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    kind: str
    channel: str = ""
    verification: Literal["unverified"]


class InsuranceOption(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    planId: str
    label: str


class InsuranceDecision(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    outcome: Literal[
        "accepted", "not_accepted", "needs_clarification", "needs_staff_task"
    ]
    participation: Literal["accepted", "not_accepted", "unknown"]
    planId: str = ""
    canonicalPlan: str = ""
    carrierCode: str = ""
    coverageType: CoverageType
    officeId: str
    allowedProviders: list[str]
    requirements: list[InsuranceRequirement]
    eligibility: Literal["not_checked"]
    canSchedule: bool
    selfPay: bool
    answer: str
    options: list[InsuranceOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.participation == "accepted" and not self.canonicalPlan:
            raise ValueError("Missing accepted plan")
        if self.canSchedule and self.participation != "accepted":
            raise ValueError("Scheduling requires an accepted plan")
        if self.canSchedule and (self.requirements or not self.allowedProviders):
            raise ValueError("Scheduling requires verified backend policy")
        return self
