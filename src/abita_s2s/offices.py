"""Office identity, trunk routing, phone numbers and each office's scheduling policy."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, NewType, get_args
from zoneinfo import ZoneInfo

from abita_s2s.insurance_contract import CoverageType

EASTERN = ZoneInfo("America/New_York")
E164 = NewType("E164", str)

SharedSchedulingOffice = Literal["hollywood", "sweetwater"]
SHARED_SCHEDULING_OFFICES: tuple[str, ...] = get_args(SharedSchedulingOffice)
HollywoodPromotedOffice = Literal["north-miami-beach-optical"]
HOLLYWOOD_PROMOTED_OFFICE: str = get_args(HollywoodPromotedOffice)[0]
SchedulingOfficeChoice = Literal[SharedSchedulingOffice, HollywoodPromotedOffice]
HOLLYWOOD_ROUTINE_OFFICE_CHOICE = (
    "needs_input: Before searching, tell the caller: Before we schedule your appointment "
    "at Hollywood, Abita Eye Group is excited to announce our newest office, at 633 "
    "Northeast 167th Street in North Miami Beach. Would you like to schedule your "
    "appointment there with Dr. Miriam Bach, or continue booking at our Hollywood "
    "location? Then search with the office the caller chooses."
)
HOLLYWOOD_ROUTINE_OFFICE_FOLLOW_UP = "needs_input: Ask whether the caller wants North Miami Beach or Hollywood, then search with that office."


@dataclass(frozen=True)
class OfficePromotion:
    visit: CoverageType
    office: str
    announcement: str
    follow_up: str


@dataclass(frozen=True)
class OfficeProfile:
    key: str
    display_name: str
    trunk_numbers: tuple[str, ...]
    greeting_name: str
    staff_tasks_enabled: bool = True
    transfer_phone: str | None = None
    product_keys: Mapping[str, str] = field(default_factory=dict)
    scheduling_offices: tuple[str, ...] = ()
    promotion: OfficePromotion | None = None


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
        scheduling_offices=SHARED_SCHEDULING_OFFICES,
        promotion=OfficePromotion(
            visit="routine_vision",
            office=HOLLYWOOD_PROMOTED_OFFICE,
            announcement=HOLLYWOOD_ROUTINE_OFFICE_CHOICE,
            follow_up=HOLLYWOOD_ROUTINE_OFFICE_FOLLOW_UP,
        ),
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
        product_keys={"+17864657479": "sweetwater-optical"},
        scheduling_offices=SHARED_SCHEDULING_OFFICES,
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
    return office.product_keys.get(e164(phone) or "", office.key)
