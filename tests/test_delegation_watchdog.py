import asyncio
import unittest
from unittest.mock import patch

from abita_s2s.runtime import delegation_watchdog
from abita_s2s.runtime.delegation_watchdog import DelegationWatchdog
from abita_s2s.runtime.silence import BackendResponses


class FakeLive:
    def __init__(self):
        self.sent = []

    def send_event(self, event):
        self.sent.append(event)


def caller(text):
    return {"type": "session.input_transcript.delta", "delta": text}


def voice(text):
    return {"type": "session.output_transcript.delta", "delta": text}


class DelegationWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patch.object(delegation_watchdog, "HANDOFF_SECONDS", 0.05).start()
        self.addCleanup(patch.stopall)
        self.live = FakeLive()
        self.backend = BackendResponses()
        self.watchdog = DelegationWatchdog(self.live, self.backend)

    async def quiet(self):
        await asyncio.sleep(0.1)

    async def test_undelegated_approval_goes_to_the_backend_with_the_false_claim(self):
        self.backend.caller_turn()
        self.watchdog.record(caller(" Sí."))
        self.watchdog.record(voice(" Listo, ya la tengo confirmada."))
        await self.quiet()
        item, create = self.live.sent
        text = item["item"]["content"][0]["text"]
        self.assertEqual(item["type"], "response.item.create")
        self.assertEqual(item["item"]["role"], "user")
        self.assertIn("The caller said: Sí.", text)
        self.assertIn("Listo, ya la tengo confirmada.", text)
        self.assertEqual(create["type"], "response.create")
        self.assertFalse(self.backend.caller_spoke_since_request)

    async def test_a_delegation_means_nothing_is_handed_off(self):
        self.watchdog.record(caller(" Yes, book it."))
        self.watchdog.record({"type": "session.delegation.created"})
        await self.quiet()
        self.assertEqual(self.live.sent, [])

    async def test_backchannels_and_fragments_are_not_handed_off(self):
        for words in (" Mm-hmm.", " I", " ,"):
            self.watchdog.record(caller(words))
            self.watchdog.record(voice(" Okay."))
            await self.quiet()
        self.assertEqual(self.live.sent, [])

    async def test_a_turn_the_voice_model_has_not_answered_waits_for_silence(self):
        self.watchdog.record(caller(" Can I talk to someone at the front desk?"))
        await self.quiet()
        self.assertEqual(self.live.sent, [])
        self.backend.caller_turn()
        self.assertTrue(self.watchdog.hand_off_held())
        item, create = self.live.sent
        text = item["item"]["content"][0]["text"]
        self.assertIn("The caller said: Can I talk to someone at the front desk?", text)
        self.assertIn("The voice assistant has not answered", text)
        self.assertEqual(create["type"], "response.create")
        self.assertFalse(self.backend.caller_spoke_since_request)
        self.assertFalse(self.watchdog.hand_off_held())

    async def test_silence_hands_off_nothing_for_backchannels_delegations_or_busy_work(
        self,
    ):
        self.watchdog.record(caller(" Mm-hmm."))
        self.assertFalse(self.watchdog.hand_off_held())
        self.watchdog.record(caller(" Can I talk to someone?"))
        self.watchdog.record({"type": "session.delegation.created"})
        self.assertFalse(self.watchdog.hand_off_held())
        self.watchdog.record(caller(" Can I talk to someone?"))
        self.backend.running.add("d1")
        self.assertFalse(self.watchdog.hand_off_held())
        self.assertEqual(self.live.sent, [])

    async def test_words_while_the_backend_works_are_not_handed_off(self):
        self.backend.running.add("d1")
        self.watchdog.record(caller(" Okay."))
        self.watchdog.record(voice(" One moment."))
        await self.quiet()
        self.backend.running.clear()
        self.watchdog.record(caller(" Thanks."))
        self.watchdog.record(voice(" You're welcome."))
        self.backend.running.add("d1")
        await self.quiet()
        self.assertEqual(self.live.sent, [])

    async def test_ongoing_speech_postpones_the_handoff(self):
        self.watchdog.record(caller(" Can you"))
        await asyncio.sleep(0.03)
        self.watchdog.record(caller(" end the call?"))
        await asyncio.sleep(0.03)
        self.watchdog.record(voice(" Sure, goodbye!"))
        await asyncio.sleep(0.03)
        self.assertEqual(self.live.sent, [])
        await self.quiet()
        self.assertIn(
            "The caller said: Can you end the call?",
            self.live.sent[0]["item"]["content"][0]["text"],
        )


if __name__ == "__main__":
    unittest.main()
