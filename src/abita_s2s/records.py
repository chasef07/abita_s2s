"""Strict, frozen wire records shared by owners, contracts and adapters."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from abita_s2s.insurance_contract import CoverageType


def provider_name(name: str) -> str:
    """The name the caller hears for a provider, however the chart spells it."""
    for old, new in (
        ("Dr. Austin Bach (Overflow)", "Dr. Bach"),
        ("Dr. Austin Bach", "Dr. Bach"),
        ("Dr. J. Licht", "Dr. Licht"),
        ("Dr. D. Noel", "Dr. Noel"),
    ):
        name = name.replace(old, new)
    return name


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    def __repr_args__(self):
        return ()


class Appointment(Record):
    id: int
    date: Text
    time: Text
    provider: str = ""
    type: str = ""
    facility: str = ""
    visitType: CoverageType | None = None
    officeId: str | None = None
    cancellationToken: str | None = None
    rescheduleToken: str | None = None

    @property
    def doctor(self) -> str:
        """The name the caller hears. Cancellation evidence reports the chart's provider."""
        return provider_name(self.provider)

    @property
    def read_back(self) -> str:
        """The date, time and doctor to read back to the caller."""
        return f"{self.date} at {self.time} Eastern with {self.doctor}"


class Candidate(Record):
    status: Literal["candidate"]
    patientId: Text
    firstName: Text
    dob: Text


class Receipt(Record):
    status: Literal["verified"]
    patientId: Text
    name: Text
    dob: Text
    insuranceCarrier: str | None = None
    insuranceCarrierId: str | None = None
    insuranceNote: str | None = None
    appointmentsStatus: Literal["found", "none", "error"]
    appointments: list[Appointment]


Category = Literal[
    "appointments",
    "documentation",
    "medication",
    "optical",
    "referrals",
    "other",
    "insurance",
    "pre_op",
    "post_op",
]
Urgency = Literal["high_priority", "normal", "non_urgent"]
