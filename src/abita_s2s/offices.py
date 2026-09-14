"""Office identity and trunk routing; business policies live with their workflows."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OfficeProfile:
    key: str
    display_name: str
    # First entry is the canonical middleware office phone; the rest are SIP aliases.
    trunk_numbers: tuple[str, ...]
    greeting: str
    staff_tasks_enabled: bool = True


SPRING_HILL = OfficeProfile(
    key="spring-hill",
    display_name="Abita Eye Group",
    trunk_numbers=("+17275919997", "+18135484830"),
    greeting="Thank you for calling Abita Eye Group. How can I help you today?",
)


OFFICES = (
    SPRING_HILL,
    OfficeProfile(
        key="crystal-river",
        staff_tasks_enabled=False,
        display_name="Eye Radiance",
        trunk_numbers=("+13523202007",),
        greeting="Thank you for calling Eye Radiance, powered by Abita Eye Group. How can I help you today?",
    ),
    OfficeProfile(
        key="hollywood",
        display_name="Abita Eye Group Hollywood",
        trunk_numbers=("+19542872010",),
        greeting="Thank you for calling Abita Eye Group. How can I help you today?",
    ),
    OfficeProfile(
        key="sweetwater",
        display_name="Abita Eye Group Sweetwater",
        trunk_numbers=(
            "+17864657475", "+17864654845", "+17866134310",
            "+17864657479", "+17864654836", "+17864654882",
        ),
        greeting="Thank you for calling Abita Eye Group. How can I help you today?",
    ),
    OfficeProfile(
        key="north-miami-beach-optical",
        display_name="North Miami Beach Optical",
        trunk_numbers=("+13055095333",),
        greeting="Thank you for calling Abita Eye Group. How can I help you today?",
    ),
)


def get_office_profile(key: str) -> OfficeProfile:
    for office in OFFICES:
        if office.key == key:
            return office
    raise ValueError("Unsupported office key")


def get_office_profile_by_phone(phone: str) -> OfficeProfile:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "1" + digits
    for office in OFFICES:
        if "+" + digits in office.trunk_numbers:
            return office
    raise ValueError("Missing or unsupported SIP trunk phone number")
