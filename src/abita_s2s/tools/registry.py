"""Every model-facing tool: how one call builds it, when the call offers it, and the speaker capability it backs."""

from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from typing import Literal, Protocol

from livekit.agents import llm

from abita_s2s.call_control import CallControl
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.tools.call_control import CallControlTools
from abita_s2s.tools.insurance import InsuranceTools
from abita_s2s.tools.knowledge import KnowledgeTools
from abita_s2s.tools.patients import PatientTools
from abita_s2s.tools.scheduling import SchedulingTools
from abita_s2s.tools.staff_tasks import StaffTaskTools

Capability = Literal[
    "Patients",
    "Appointments",
    "Insurance",
    "Office information",
    "Staff assistance",
    "Call completion",
]


class Writer(Protocol):
    def close_admission(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Owners:
    """One call's office and the owners its tools delegate to."""

    office: OfficeProfile
    knowledge: OfficeKnowledge
    _: KW_ONLY
    resolver: PatientResolver | None = None
    scheduling: Scheduling | None = None
    insurance: InsuranceRegistration | None = None
    staff_tasks: StaffTasks | None = None
    call_control: CallControl | None = None

    def writers(self) -> list[Writer]:
        """Every owner that admits writes, which shutdown closes and drains in this order."""
        return [
            owner
            for owner in (
                self.resolver,
                self.scheduling,
                self.insurance,
                self.staff_tasks,
                self.call_control,
            )
            if owner is not None
        ]


@dataclass(frozen=True)
class Entry:
    """One tool or toolset; ``build`` returns None when this call does not offer it."""

    capabilities: tuple[Capability, ...]
    build: Callable[[Owners], llm.Tool | llm.Toolset | None]


TOOLS: tuple[Entry, ...] = (
    Entry(
        ("Appointments",),
        lambda o: (
            SchedulingTools(o.scheduling).list_available_appointments
            if o.scheduling
            else None
        ),
    ),
    Entry(
        ("Appointments",),
        lambda o: (
            SchedulingTools(o.scheduling).book_appointment if o.scheduling else None
        ),
    ),
    Entry(
        ("Appointments",),
        lambda o: (
            SchedulingTools(o.scheduling).cancel_appointment if o.scheduling else None
        ),
    ),
    Entry(
        ("Appointments",),
        lambda o: (
            SchedulingTools(o.scheduling).reschedule_appointment
            if o.scheduling
            else None
        ),
    ),
    Entry(
        ("Staff assistance",),
        lambda o: (
            StaffTaskTools(o.staff_tasks).save_staff_task
            if o.office.staff_tasks_enabled
            else None
        ),
    ),
    Entry(
        ("Call completion", "Staff assistance"),
        lambda o: CallControlTools(o.call_control) if o.call_control else None,
    ),
    Entry(
        ("Patients",),
        lambda o: PatientTools(o.resolver, o.insurance, o.scheduling).add_patient,
    ),
    Entry(("Insurance",), lambda o: InsuranceTools(o.insurance).check_insurance),
    Entry(
        ("Patients",),
        lambda o: PatientTools(o.resolver, o.insurance, o.scheduling).resolve_patient,
    ),
    Entry(
        ("Office information",),
        lambda o: KnowledgeTools(o.knowledge).search_office_knowledge,
    ),
    Entry(("Insurance",), lambda o: InsuranceTools(o.insurance).update_insurance),
)


def offer(owners: Owners) -> list[llm.Tool | llm.Toolset]:
    """The tools this call offers, in registry order."""
    return [tool for entry in TOOLS if (tool := entry.build(owners)) is not None]
