"""Office identity and trunk routing; business policies live with their workflows."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OfficeProfile:
    key: str
    display_name: str
    # First entry is the canonical middleware office phone; the rest are SIP aliases.
    trunk_numbers: tuple[str, ...]
    greeting_name: str
    staff_tasks_enabled: bool = True
    transfer_phone: str | None = None


SPRING_HILL = OfficeProfile(
    key="spring-hill",
    display_name="Abita Eye Group",
    trunk_numbers=("+17275919997", "+18135484830"),
    greeting_name="Abita Eye Group",
)


OFFICES = (
    SPRING_HILL,
    OfficeProfile(
        transfer_phone="tel:+13527941244",
        key="crystal-river",
        staff_tasks_enabled=False,
        display_name="Eye Radiance",
        trunk_numbers=("+13523202007",),
        greeting_name="Eye Radiance, powered by Abita Eye Group",
    ),
    OfficeProfile(
        key="hollywood",
        display_name="Abita Eye Group Hollywood",
        trunk_numbers=("+19542872010",),
        greeting_name="Abita Eye Group",
    ),
    OfficeProfile(
        key="sweetwater",
        display_name="Abita Eye Group Sweetwater",
        trunk_numbers=(
            "+17864657475", "+17864654845", "+17866134310",
            "+17864657479", "+17864654836", "+17864654882",
        ),
        greeting_name="Abita Eye Group",
    ),
    OfficeProfile(
        key="north-miami-beach-optical",
        display_name="North Miami Beach Optical",
        trunk_numbers=("+13055095333",),
        greeting_name="Abita Eye Group",
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
