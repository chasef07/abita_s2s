"""Office identity and trunk routing; business policies live with their workflows."""

import re
from dataclasses import dataclass
from typing import Literal, NewType, get_args
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
E164 = NewType("E164", str)

SharedSchedulingOffice = Literal["hollywood", "sweetwater"]
SHARED_SCHEDULING_OFFICES: tuple[str, ...] = get_args(SharedSchedulingOffice)
HollywoodPromotedOffice = Literal["north-miami-beach-optical"]
HOLLYWOOD_PROMOTED_OFFICE: str = get_args(HollywoodPromotedOffice)[0]
SchedulingOfficeChoice = Literal[SharedSchedulingOffice, HollywoodPromotedOffice]


@dataclass(frozen=True)
class OfficeProfile:
    key: str
    display_name: str
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
            "+17864657475",
            "+17864654845",
            "+17866134310",
            "+17864657479",
            "+17864654836",
            "+17864654882",
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


def same_office(office_id: str, key: str) -> bool:
    return office_id.replace("_", "-") == key


def e164(phone: str | None) -> E164 | None:
    """A number with a leading + keeps its country code; ten bare digits are US."""
    text = (phone or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and not text.startswith("+"):
        digits = "1" + digits
    number = "+" + digits
    return E164(number) if re.fullmatch(r"\+[1-9][0-9]{7,14}", number) else None


def get_office_profile_by_phone(phone: str) -> OfficeProfile:
    for office in OFFICES:
        if e164(phone) in office.trunk_numbers:
            return office
    raise ValueError("Missing or unsupported SIP trunk phone number")


def get_product_office_key(phone: str) -> str:
    office = get_office_profile_by_phone(phone)
    if e164(phone) == "+17864657479":
        return "sweetwater-optical"
    return office.key
