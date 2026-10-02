"""One attempt per chart/insurance write; ambiguous responses are never retried."""

import asyncio
from typing import Literal

import httpx

from abita_s2s.insurance_contract import InsuranceDecision
from abita_s2s.integrations.middleware import Middleware
from abita_s2s.offices import same_office
from abita_s2s.records import Record, Text


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


class RegistrationMiddleware(Middleware):
    async def check(
        self, office: str, plan: str, coverage: str, dob: str = ""
    ) -> InsuranceDecision | None:
        if not self._configured:
            return None
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._send(
                    "/api/insurance/decision",
                    {
                        "office": office,
                        "plan": plan,
                        "coverageType": coverage,
                        "dob": dob,
                    },
                )
                response.raise_for_status()
                decision = InsuranceDecision.model_validate(response.json())
                if (
                    not same_office(decision.officeId, office)
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
        if not self._configured:
            return WriteFailure(status="failed", reason="not_configured")
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._send(path, {**payload, "office": office})
            body = response.json()
            failed = isinstance(body, dict) and body.get("status") == "error"
            reason = failed and (
                body.get("message") or body.get("outcome") or "backend_failure"
            )
            if receipt is UpdatedReceipt and failed:
                effect = body.get("effect")
                return WriteFailure(
                    status="failed"
                    if effect == "no_effect"
                    else "partial"
                    if effect == "partial"
                    else "uncertain",
                    reason=reason,
                )
            if not response.is_success:
                return WriteFailure(status="uncertain", reason="http_failure")
            if failed:
                safe = receipt is CreationReceipt and body.get("outcome") in (
                    "validation_failed",
                    "rejected",
                    "reconciled_failure",
                    "unavailable",
                    "failed",
                )
                return WriteFailure(
                    status="failed" if safe else "uncertain", reason=reason
                )
            result = receipt.model_validate(body)
            decision = result.insuranceDecision
            if decision is not None and (
                not same_office(decision.officeId, office)
                or decision.coverageType != payload.get("coverageType", "medical")
                or decision.canonicalPlan != payload["insurance"]
                or (
                    "insurancePlanId" in payload
                    and decision.planId != payload["insurancePlanId"]
                )
            ):
                return WriteFailure(
                    status="uncertain", reason="mismatched_insurance_decision"
                )
            return result
        except (httpx.HTTPError, TimeoutError, ValueError):
            return WriteFailure(status="uncertain", reason="unverified_write")
