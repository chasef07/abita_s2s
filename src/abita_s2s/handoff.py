"""Resolve trusted office destinations and preserve handoff admission identity."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import httpx

from abita_s2s.config import HandoffConfig
from abita_s2s.offices import get_office_profile, get_product_office_key
from abita_s2s.state import CallState


class AdmissionRejected(Exception):
    """Admission definitively did not write; a bounded retry remains safe."""


@dataclass(frozen=True)
class HandoffTarget:
    destination: str
    headers: dict[str, str] = field(default_factory=dict)
    admitted: bool = False


class HandoffAdmission:
    def __init__(
        self, state: CallState, client: httpx.AsyncClient, config: HandoffConfig | None
    ):
        self.state = state
        self.client = client
        self.config = config
        self._payload = None

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
        if self.config is None:
            raise AdmissionRejected("Handoff configuration is incomplete")
        if self._payload is None:
            identity = {
                "practiceId": self.config.practice_id,
                "officeKey": get_product_office_key(
                    call.called_number or office.trunk_numbers[0]
                ),
                "sourceCallId": call.call_id,
            }
            contact = {
                "phone": call.caller_phone or "",
                "phoneSource": "livekit.sip.callerPhoneNumber",
            }
            self._payload = {
                **identity,
                "contact": contact,
                "idempotencyKey": hashlib.sha256(
                    json.dumps(identity, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        response = await self.client.post(
            self.config.url,
            json=self._payload,
            headers={"Authorization": f"Bearer {self.config.secret}"},
            timeout=2,
        )
        if 400 <= response.status_code < 500 and response.status_code not in (408, 409):
            raise AdmissionRejected(
                f"Handoff admission rejected: {response.status_code}"
            )
        response.raise_for_status()
        body = response.json()
        expires = datetime.fromisoformat(body["expiresAt"])
        remaining = (expires - datetime.now(UTC)).total_seconds()
        if remaining <= 0 or remaining > 300:
            raise ValueError("Invalid handoff expiration")
        target = body["sipDestination"]
        if (
            not isinstance(target, str)
            or not target.startswith("sip:")
            or "@" not in target
            or any(c.isspace() for c in target)
        ):
            raise ValueError("Invalid SIP destination")
        UUID(body["id"])
        if target[4:].split("@", 1)[0] != "acuity-handoff":
            raise ValueError("Invalid Product destination")
        return HandoffTarget(target, {}, admitted=True)
