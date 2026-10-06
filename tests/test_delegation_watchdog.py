import asyncio
import unittest
from unittest.mock import patch

from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.runtime import delegation_watchdog
from abita_s2s.runtime.delegation_watchdog import DelegationWatchdog
from abita_s2s.runtime.observability import ObservedGPTLiveModel, ObservedGPTLiveSession


class FakeLive:
    def __init__(self):
        self.backend_busy = False
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
        self.watchdog = DelegationWatchdog(self.live)

    async def quiet(self):
        await asyncio.sleep(0.1)

    async def test_undelegated_approval_goes_to_the_backend_with_the_false_claim(self):
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

    async def test_a_delegation_means_nothing_is_handed_off(self):
        self.watchdog.record(caller(" Yes, book it."))
        self.watchdog.record({"type": "session.delegation.created"})
        await self.quiet()
        self.assertEqual(self.live.sent, [])

    async def test_backchannels_and_fragments_are_not_handed_off(self):
        for words in (" Mm-hmm.", " I", " ,"):
            self.watchdog.record(caller(words))
            await self.quiet()
        self.assertEqual(self.live.sent, [])

    async def test_words_while_the_backend_works_are_not_handed_off(self):
        self.live.backend_busy = True
        self.watchdog.record(caller(" Okay."))
        await self.quiet()
        self.live.backend_busy = False
        self.watchdog.record(caller(" Thanks."))
        self.live.backend_busy = True
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


class WiringTests(unittest.TestCase):
    def test_backend_busy_reads_the_plugin_backend_state(self):
        live = object.__new__(ObservedGPTLiveSession)
        live._backend_running_responses, live._backend_open_calls = {}, set()
        live._backend_response_pending = False
        self.assertFalse(live.backend_busy)
        live._backend_open_calls = {"call_1"}
        self.assertTrue(live.backend_busy)

    def test_each_live_session_gets_a_watchdog(self):
        with patch.object(GPTLiveSession, "__init__", lambda self, model: None):
            model = ObservedGPTLiveModel(api_key="offline")
            with (
                patch.object(ObservedGPTLiveSession, "__init__", lambda *a: None),
                patch.object(GPTLiveSession, "on") as on,
            ):
                model.session()
        handlers = [call.args[1] for call in on.call_args_list]
        self.assertTrue(
            any(
                getattr(h, "__self__", None).__class__ is DelegationWatchdog
                for h in handlers
            )
        )


if __name__ == "__main__":
    unittest.main()
