"""Run only against the ephemeral Go mocked-provider contract server."""

import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace

import httpx
from test_patient_resolution import call_state
from test_scheduling import verified

from abita_s2s.offices import get_office_profile
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP


async def main(url):
    assert url.startswith("http://127.0.0.1:"), "Contract runner requires a local fixture server"
    async with httpx.AsyncClient() as client:
        fixture = (await client.get(url + "/fixture")).json()
        config = SimpleNamespace(middleware_url=url, middleware_token="test-auth")
        state = call_state(None)
        owner = Scheduling(state, SchedulingHTTP(client, config),
                           now=lambda: datetime.fromisoformat(fixture["now"]))
        context = SimpleNamespace(userdata=state, function_call=SimpleNamespace(call_id="contract"))
        try:
            # Exercise the real patient response, including office/type/token metadata.
            loaded = (await client.post(url + "/api/patient/resolve", headers={"Authorization": "test-auth"},
                                        json={"patientId": "12345", "office": "Spring Hill"})).json()
            assert loaded["appointments"][0]["visitType"] == "medical", loaded
            assert loaded["appointments"][0]["officeId"] == "spring_hill"
            assert loaded["appointments"][0]["cancellationToken"]
            assert loaded["appointments"][0]["rescheduleToken"]
            verified(state, patient_id="12345", dob="01/15/1980",
                     appointmentsStatus=loaded["appointmentsStatus"],
                     appointments=loaded["appointments"])
            old_ref = owner.appointments()[0]["appointmentRef"]
            available = await owner.availability("medical", "2026-06-03")
            assert available["outcome"] == "found", available
            slot_ref = available["slots"][0]["appointmentSlotRef"]
            args = dict(oldAppointmentRef=old_ref, appointmentSlotRef=slot_ref,
                        appointmentReason="Medical follow up", referringDoctor="none", readBack=True)
            result = await owner.reschedule_appointment(context, **args)
            expected = {"success": "success:", "partial": "blocked:", "failure": "blocked:"}[fixture["scenario"]]
            assert result.startswith(expected), result
            ids = [a.id for a in state.patient.active.appointments]
            expected_ids = {"success": [98765], "partial": [54321, 98765], "failure": [54321]}[fixture["scenario"]]
            assert ids == expected_ids, (ids, expected_ids)
            repeated = await owner.reschedule_appointment(context, **args)
            if fixture["scenario"] == "failure":
                assert repeated.startswith("needs_input:"), repeated
            else:
                assert repeated == result
            # Repeated tools are handled by call-local receipts. The HTTP write
            # is single-shot and must never be retried after a lost response.
            for appointment in state.patient.active.appointments:
                if appointment.id == 98765:
                    assert appointment.visitType == "medical"
                    assert appointment.officeId == "spring_hill"
                    assert appointment.cancellationToken and appointment.rescheduleToken
            # Unsupported office/visit combinations must remain distinguishable
            # from a full calendar across the actual middleware response.
            for office, visit in (("crystal-river", "routine_vision"), ("north-miami-beach-optical", "medical")):
                office_state = call_state(None, get_office_profile(office))
                verified(office_state, patient_id="12345", dob="01/15/1980", visit=visit)
                office_owner = Scheduling(office_state, SchedulingHTTP(client, config),
                                          now=lambda: datetime.fromisoformat(fixture["now"]))
                try:
                    response = await office_owner.list_available_appointments(
                        SimpleNamespace(userdata=office_state), visitType=visit,
                    )
                    assert response.startswith("blocked: No providers are eligible"), response
                    assert "Searched" not in response and "other dates" not in response, response
                finally:
                    await office_owner.aclose()
            print(f"{fixture['scenario']}: Python owner -> authenticated Go handlers -> mocked writes -> receipts/state/retry verified")
        finally:
            await owner.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
