"""One attempt per chart/insurance write; ambiguous responses are never retried."""

import asyncio
from typing import Literal

import httpx

from abita_s2s.config import Config
from abita_s2s.eligibility_contract import EligibilityInput, EligibilityResult
from abita_s2s.insurance_contract import InsuranceDecision
from abita_s2s.integrations.patient_middleware import Record, Text
from abita_s2s.offices import get_office_profile
from abita_s2s.name_matcher import parse_dob


class CreationReceipt(Record):
    insuranceDecision: InsuranceDecision | None = None
    status: Literal["created", "partial"]
    patientId: Text
    name: Text
    dob: Text


class UpdatedReceipt(Record):
    insuranceDecision: InsuranceDecision | None = None
    status: Literal["updated"]
    effect: Literal["completed"]
    patientId: Text
    newInsurance: Text


class WriteFailure(Record):
    status: Literal["failed", "partial", "uncertain"]
    reason: str


class RegistrationMiddleware:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: Config,
        *,
        deadline: float = 20,
        eligibility_deadline: float = 30,
    ):
        self._client = client
        self._config = config
        self._deadline = deadline
        # Middleware allows Stedi 25 seconds; leave time for transport and decoding.
        self._eligibility_deadline = eligibility_deadline

    async def eligibility(
        self, office: str, details: EligibilityInput
    ) -> EligibilityResult | None:
        if not self._config.middleware_url or not self._config.middleware_token:
            return None
        try:
            async with asyncio.timeout(self._eligibility_deadline):
                response = await self._client.post(
                    self._config.middleware_url.rstrip("/") + "/api/eligibility/check",
                    headers={"Authorization": self._config.middleware_token},
                    json={
                        **details.model_dump(),
                        "office": get_office_profile(office).trunk_numbers[0],
                    },
                    timeout=self._eligibility_deadline,
                    follow_redirects=False,
                )
                response.raise_for_status()
                result = EligibilityResult.model_validate(response.json())
                if result.officeId.replace("_", "-") != office:
                    return None
                if result.status in ("active", "inactive") and (
                    result.identity is None
                    or result.identity.reviewRequired
                    or result.identity.status
                    not in ("exact_name_dob", "matched_with_name_correction")
                ):
                    return None
                if (
                    result.identity
                    and result.identity.status == "matched_with_name_correction"
                ):
                    matched = result.matchedPatient
                    requested_dob = parse_dob(details.dob)
                    if (
                        result.identity.reviewRequired
                        or matched is None
                        or requested_dob is None
                        or requested_dob.strftime("%Y%m%d") != matched.dateOfBirth
                        or not matched.memberId
                        or "".join(details.memberId.split()).upper()
                        != "".join(matched.memberId.split()).upper()
                    ):
                        return None
                return result
        except (httpx.HTTPError, TimeoutError, ValueError):
            return None

    async def check(
        self, office: str, plan: str, coverage: str, dob: str = ""
    ) -> InsuranceDecision | None:
        if not self._config.middleware_url or not self._config.middleware_token:
            return None
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._client.post(
                    self._config.middleware_url.rstrip("/") + "/api/insurance/decision",
                    headers={"Authorization": self._config.middleware_token},
                    json={
                        "office": get_office_profile(office).trunk_numbers[0],
                        "plan": plan,
                        "coverageType": coverage,
                        "dob": dob,
                    },
                    timeout=self._deadline,
                    follow_redirects=False,
                )
                response.raise_for_status()
                decision = InsuranceDecision.model_validate(response.json())
                if (
                    decision.officeId.replace("_", "-") != office
                    or decision.coverageType != coverage
                ):
                    return None
                return decision
        except (httpx.HTTPError, TimeoutError, ValueError):
            return None

    async def create(
        self, office: str, payload: dict
    ) -> CreationReceipt | WriteFailure:
        return await self._write("/api/add-patient", office, payload, CreationReceipt)

    async def update(self, office: str, payload: dict) -> UpdatedReceipt | WriteFailure:
        return await self._write(
            "/api/patient/update-insurance", office, payload, UpdatedReceipt
        )

    async def _write(self, path, office, payload, receipt):
        if not self._config.middleware_url or not self._config.middleware_token:
            return WriteFailure(status="failed", reason="not_configured")
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._client.post(
                    self._config.middleware_url.rstrip("/") + path,
                    headers={"Authorization": self._config.middleware_token},
                    json={
                        **payload,
                        "office": get_office_profile(office).trunk_numbers[0],
                    },
                    timeout=self._deadline,
                    follow_redirects=False,
                )
            body = response.json()
            if (
                receipt is UpdatedReceipt
                and isinstance(body, dict)
                and body.get("status") == "error"
            ):
                effect = body.get("effect")
                return WriteFailure(
                    status="failed"
                    if effect == "no_effect"
                    else "partial"
                    if effect == "partial"
                    else "uncertain",
                    reason=body.get("message")
                    or body.get("outcome")
                    or "backend_failure",
                )
            if not response.is_success:
                return WriteFailure(status="uncertain", reason="http_failure")
            if isinstance(body, dict) and body.get("status") == "error":
                # Only these outcomes prove that chart creation made no chart.
                # An update can already have ended the old plan before failing.
                safe = receipt is CreationReceipt and body.get("outcome") in (
                    "validation_failed",
                    "rejected",
                    "reconciled_failure",
                    "unavailable",
                    "failed",
                )
                return WriteFailure(
                    status="failed" if safe else "uncertain",
                    reason=body.get("message")
                    or body.get("outcome")
                    or "backend_failure",
                )
            result = receipt.model_validate(body)
            decision = result.insuranceDecision
            if decision is not None and (
                decision.officeId.replace("_", "-") != office
                or decision.coverageType != payload.get("coverageType", "medical")
                or decision.canonicalPlan != payload["insurance"]
            ):
                return WriteFailure(
                    status="uncertain", reason="mismatched_insurance_decision"
                )
            return result
        except (httpx.HTTPError, TimeoutError, ValueError):
            return WriteFailure(status="uncertain", reason="unverified_write")
