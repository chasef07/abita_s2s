"""Resolve trusted office destinations and preserve handoff admission identity."""

from dataclasses import dataclass, field

from abita_s2s.integrations.product import Product
from abita_s2s.offices import get_office_profile, get_product_office_key
from abita_s2s.state import CallState


class AdmissionRejected(Exception):
    """Admission definitively did not write; a bounded retry remains safe."""


class AdmissionUnconfirmed(Exception):
    """Admission may have written; the handoff must not be retried."""


@dataclass(frozen=True)
class HandoffTarget:
    destination: str
    headers: dict[str, str] = field(default_factory=dict)
    admitted: bool = False


class HandoffAdmission:
    def __init__(self, state: CallState, product: Product):
        self.state = state
        self.product = product

    async def resolve(self) -> HandoffTarget:
        call = self.state.call
        office = get_office_profile(call.called_office_key)
        if office.transfer_phone:
            return HandoffTarget(
                office.transfer_phone,
                {
                    "X-Acuity-Caller-Phone": call.caller_phone or "",
                    "X-Acuity-Handoff": "call-center",
                    "X-Acuity-Handoff-Target": office.transfer_phone,
                    "X-Acuity-LiveKit-Call-Id": call.call_id,
                    "X-Acuity-Office-Key": office.key,
                    "X-Acuity-Trunk-Phone": call.called_number or "",
                },
            )
        grant = await self.product.admit(
            get_product_office_key(call.called_number or office.trunk_numbers[0]),
            call.call_id,
            call.caller_phone or "",
        )
        if grant == "not_configured":
            raise AdmissionRejected("Handoff configuration is incomplete")
        if grant == "rejected":
            raise AdmissionRejected("Handoff admission rejected")
        if grant == "unconfirmed":
            raise AdmissionUnconfirmed("Handoff admission is unconfirmed")
        return HandoffTarget(grant.sipDestination, {}, admitted=True)
