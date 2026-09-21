"""Real Python owners against authenticated Go handlers and mocked provider records."""

import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace

import httpx
from test_patient_resolution import call_state

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_state import accepted_insurance, insurance_ready
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.registration_middleware import RegistrationMiddleware
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import Inventory, SchedulingHTTP


async def main(url):
    assert url.startswith("http://127.0.0.1:"), (
        "Only the local fixture server is allowed"
    )
    requests = []

    async def record(request):
        requests.append(request.url.path)

    async with httpx.AsyncClient(event_hooks={"request": [record]}) as client:
        fixture = (await client.get(url + "/fixture")).json()
        scenario = fixture["scenario"]
        config = SimpleNamespace(middleware_url=url, middleware_token="test-auth")
        state = call_state(None)
        resolver = PatientResolver(state, PatientMiddleware(client, config))
        insurance = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, config)
        )
        scheduler = Scheduling(
            state,
            SchedulingHTTP(client, config),
            now=lambda: datetime.fromisoformat(fixture["now"]),
        )
        context = SimpleNamespace(
            userdata=state, function_call=SimpleNamespace(call_id="contract")
        )
        try:
            resolved = await resolver.resolve("Jane", "01/15/1980")
            assert resolved["outcome"] == "verified", resolved
            assert state.patient.active.patientId == "12345"
            assert "insPlanId" not in state.patient.active.model_dump()
            assert "respPartyId" not in state.patient.active.model_dump()
            if scenario == "availability_invalid_input":
                result = await scheduler.http.availability(
                    {
                        "office": "Spring Hill",
                        "patientId": "12345",
                        "dob": "01/15/1980",
                        "visitType": "medical",
                        "startDate": "bad-date",
                        "rangeDays": 14,
                    }
                )
                assert isinstance(result, Inventory), result
                assert result.status == "error" and result.outcome == "invalid_input", (
                    result
                )
                assert result.message and not result.shouldRetrySameSearch
            elif scenario.startswith("availability_"):
                result = await scheduler.availability("medical", "2026-06-03")
                expected = (
                    "unsupported"
                    if scenario == "availability_policy_blocked"
                    else "availability_failed"
                )
                assert result["outcome"] == expected, result
                assert result["retry_same_search"] == (
                    scenario == "availability_read_failure"
                ), result
                if scenario == "availability_policy_blocked":
                    repeated = await scheduler.list_available_appointments(
                        context, visitType="medical", startDate="2026-06-03"
                    )
                    assert repeated.startswith("blocked:"), repeated
                    assert "Retry this search once" not in repeated
                    assert requests.count("/api/scheduler/slots") == 1
            elif scenario.startswith("insurance_"):
                await insurance.check("Meritain Health", "medical")
                assert accepted_insurance(state) is not None
                original = state.patient.active
                result = await insurance.update("H123")
                expected = {
                    "insurance_completed": "updated",
                    "insurance_no_current_plan": "updated",
                    "insurance_no_effect": "failed",
                    "insurance_missing_refs": "failed",
                    "insurance_partial": "partial",
                    "insurance_uncertain": "uncertain",
                }[scenario]
                assert result["outcome"] == expected, result
                assert requests.count("/api/patient/resolve") == 1, requests
                if expected == "updated":
                    assert state.patient.active.insuranceCarrier == "Meritain Health"
                    assert accepted_insurance(state) is not None
                    assert insurance_ready(state)
                    assert await insurance.update("H123") == result
                elif expected == "failed":
                    assert state.patient.active is original
                    assert not state.insurance.write_uncertain
                    assert "Do not repeat" not in result["answer"]
                else:
                    assert not insurance_ready(state)
                    assert state.insurance.write_uncertain
                    if expected == "partial":
                        assert "replacement was not attached" in result["answer"]
                    repeated = await insurance.update("corrected-member")
                    assert repeated["outcome"] == expected, repeated
                assert requests.count("/api/patient/update-insurance") == 1
            else:
                if scenario == "cancellation_invalid_token":
                    active = state.patient.active
                    state.patient.active = active.model_copy(
                        update={
                            "appointments": [
                                appt.model_copy(
                                    update={"cancellationToken": "tampered"}
                                )
                                for appt in active.appointments
                            ]
                        }
                    )
                ref = scheduler.appointments()[0]["appointmentRef"]
                result = await scheduler.cancel_appointment(
                    context, appointmentRef=ref, readBack=True
                )
                if scenario == "cancellation_completed":
                    assert result.startswith("success:"), result
                    assert state.patient.active.appointments == []
                    assert (
                        await scheduler.cancel_appointment(
                            context, appointmentRef=ref, readBack=True
                        )
                        == result
                    )
                else:
                    assert not result.startswith("success:"), result
                    assert [a.id for a in state.patient.active.appointments] == [54321]
                    if scenario == "cancellation_uncertain":
                        assert "outcome could not be confirmed" in result, result
                        assert (
                            await scheduler.cancel_appointment(
                                context, appointmentRef=ref, readBack=True
                            )
                            == result
                        )
                    else:
                        assert "outcome could not be confirmed" not in result, result
                        assert state.patient.active.appointmentsStatus == "error"
                        repeated = await scheduler.cancel_appointment(
                            context, appointmentRef=ref, readBack=True
                        )
                        assert repeated.startswith("needs_input:"), repeated
                assert requests.count("/api/appointment/cancel") == 1
            print(
                f"{scenario}: real HTTP contract, owner result/state, and mutation fence verified"
            )
        finally:
            await scheduler.aclose()
            await insurance.aclose()
            await resolver.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
