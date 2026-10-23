"""The offline replay judges transcripts deterministically, and its fixture backend speaks the real contracts."""

import unittest
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlsplit

from abita_s2s.insurance_contract import InsuranceDecision
from abita_s2s.integrations.patient_middleware import (
    NotFound,
    PatientMiddleware,
    Receipt,
    ResolveRequest,
)
from abita_s2s.integrations.product import KnowledgeResult, Product
from abita_s2s.integrations.registration_middleware import (
    DecisionRequest,
    RegistrationMiddleware,
)
from evals.replay.checks import (
    Agent,
    Call,
    Caller,
    Output,
    delegates,
    ends_call,
    first_argument,
    grounded,
    no_unstarted_action,
)
from evals.replay.scenarios import SCENARIOS
from evals.replay.world import World, config

ROOT = Path(__file__).parents[1]
ASKS = Caller(0, "Can I talk to someone at the front desk?")


def search(call_id="c1"):
    return [
        Call("search_office_knowledge", {"query": "address"}, call_id),
        Output("search_office_knowledge", "success: 100 Fixture Way", call_id),
    ]


class ChecksTests(unittest.TestCase):
    def test_flags_a_check_with_no_tool_behind_it(self):
        events = [ASKS, Agent("One moment, let me check."), Agent("What is it about?")]
        self.assertEqual(
            no_unstarted_action()(events),
            "narrated an unstarted action: 'One moment, let me check.'",
        )

    def test_flags_the_availability_checks_153_found(self):
        for said in (
            "One moment, I'll see if someone is available.",
            "One moment, checking your file.",
            "Déjeme revisar si está disponible.",
        ):
            with self.subTest(said):
                self.assertIsNotNone(no_unstarted_action()([ASKS, Agent(said)]))

    def test_accepts_a_check_the_backend_starts(self):
        events = [ASKS, Agent("Let me check."), *search(), Agent("We're at 100.")]
        self.assertIsNone(no_unstarted_action()(events))

    def test_accepts_the_announcement_a_running_transfer_speaks(self):
        events = [
            ASKS,
            Call("transfer_call", {}, "t1"),
            Agent("I'm transferring you now."),
            Output("transfer_call", "unavailable: No transfer was made.", "t1"),
        ]
        self.assertIsNone(no_unstarted_action()(events))

    def test_a_transfer_claim_needs_the_transfer_tool(self):
        events = [ASKS, Agent("I'm transferring you."), *search()]
        self.assertIsNotNone(no_unstarted_action()(events))

    def test_a_transfer_does_not_back_a_check(self):
        events = [
            ASKS,
            Agent("Let me check whether Maria is available."),
            Call("transfer_call", {}, "t1"),
            Output("transfer_call", "unavailable: No transfer was made.", "t1"),
        ]
        self.assertIsNotNone(no_unstarted_action()(events))

    def test_a_claim_after_the_tool_finished_needs_a_new_tool(self):
        events = [ASKS, *search(), Agent("We're at 100. Let me check parking.")]
        self.assertIsNotNone(no_unstarted_action()(events))

    def test_an_offer_claims_nothing(self):
        events = [ASKS, Agent("Okay. Would you like me to try connecting you again?")]
        self.assertIsNone(no_unstarted_action()(events))

    def test_grounded_ignores_the_greeting_and_flags_an_early_fact(self):
        check = grounded("search_office_knowledge", r"\d", "an address")
        greeting = Agent("Thanks for calling suite 1.")
        self.assertIsNone(check([greeting, ASKS, Agent("One moment."), *search()]))
        self.assertEqual(
            check([greeting, ASKS, Agent("We're at 100 Main."), *search()]),
            'stated an address before search_office_knowledge: "We\'re at 100 Main."',
        )

    def test_delegates_only_within_the_turn(self):
        check = delegates("search_office_knowledge", 0)
        self.assertIsNone(check([ASKS, *search(), Caller(1, "Thanks.")]))
        self.assertEqual(
            check([ASKS, Agent("One moment."), Caller(1, "Hello?"), *search()]),
            "stalled: no search_office_knowledge after caller turn 0",
        )
        self.assertEqual(
            delegates("search_office_knowledge", 2)([ASKS]),
            "the call never reached caller turn 2",
        )

    def test_ends_call_only_after_the_final_turn(self):
        check = ends_call(1)
        goodbye = Caller(1, "Bye.")
        end = Call("end_call", {}, "e1")
        self.assertIsNone(check([ASKS, goodbye, end]))
        self.assertEqual(check([ASKS, end]), "ended the call before caller turn 1")
        self.assertEqual(
            check([ASKS, goodbye]), "did not end the call after the caller's goodbye"
        )

    def test_first_argument_reads_the_first_call(self):
        check = first_argument("check_insurance", "coverageType", "routine_vision")
        medical = Call("check_insurance", {"coverageType": "medical"}, "i1")
        vision = Call("check_insurance", {"coverageType": "routine_vision"}, "i2")
        self.assertIsNone(check([ASKS, vision, medical]))
        self.assertEqual(
            check([ASKS, medical, vision]),
            "check_insurance coverageType was 'medical', expected 'routine_vision'",
        )
        self.assertEqual(check([ASKS]), "never called check_insurance")


class WorldTests(unittest.IsolatedAsyncioTestCase):
    async def test_answers_through_the_real_contracts(self):
        world = World()
        settings = config("offline")
        async with world.client() as client:
            found = await PatientMiddleware(client, settings).resolve(
                ResolveRequest(firstName="Lana", dob="04/16/1992", office="spring-hill")
            )
            missing = await PatientMiddleware(client, settings).resolve(
                ResolveRequest(phone="+13525550147", office="spring-hill")
            )
            decision = await RegistrationMiddleware(client, settings).check(
                DecisionRequest(
                    office="spring-hill",
                    plan="Aetna",
                    coverageType="routine_vision",
                    dob="",
                )
            )
            product = Product(client, settings)
            hours = await product.search("spring-hill", "Monday office hours")
            parking = await product.search("spring-hill", "Is there parking?")
        self.assertIsInstance(found, Receipt)
        self.assertIsInstance(missing, NotFound)
        self.assertIsInstance(decision, InsuranceDecision)
        self.assertIsInstance(hours, KnowledgeResult)
        self.assertIsInstance(parking, KnowledgeResult)
        self.assertEqual([p.title for p in hours.passages], ["Hours"])
        self.assertEqual(parking.outcome, "no_relevant_information")
        self.assertEqual(world.unrouted, [])

    async def test_records_a_request_it_has_no_route_for(self):
        world = World()
        async with world.client() as client:
            reply = await client.post(
                "https://middleware.fixture.invalid/api/x", json={}
            )
        self.assertEqual(reply.status_code, 501)
        self.assertEqual(
            world.unrouted, ["POST https://middleware.fixture.invalid/api/x"]
        )


class IsolationTests(unittest.TestCase):
    def test_only_the_model_key_is_real(self):
        settings = config("model-key")
        for field in fields(settings):
            value = getattr(settings, field.name)
            if isinstance(value, str) and "://" in value:
                self.assertTrue(urlsplit(value).hostname.endswith(".invalid"), value)
        self.assertIsNone(settings.handoff)
        self.assertIsNone(settings.interaction_url)

    def test_never_loads_production_settings_or_a_worker(self):
        for path in sorted((ROOT / "evals/replay").glob("*.py")):
            source = path.read_text()
            for banned in (
                "load_config",
                "load_dotenv",
                "abita_s2s.main",
                "cli.run_app",
            ):
                self.assertNotIn(banned, source, path.name)

    def test_scenario_names_are_unique(self):
        names = [scenario.name for scenario in SCENARIOS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
