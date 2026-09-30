import unittest
from unittest.mock import patch

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.runtime.observability import ObservedGPTLiveModel
from abita_s2s.runtime.silence import CHECK_IN, BackendResponses, check_in_on_silence


def backend_event(kind, delegation_id="d1"):
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": {"type": kind},
    }


class SilenceCheckInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = BackendResponses()
        self.session = AgentSession(llm=ObservedGPTLiveModel(api_key="offline"))
        check_in_on_silence(self.session, self.backend)
        self.reset = patch.object(self.session, "reset_away_timer").start()
        self.reply = patch.object(self.session, "generate_reply").start()
        self.addCleanup(patch.stopall)

    def user(self, new):
        self.session.emit(
            "user_state_changed",
            UserStateChangedEvent(old_state="listening", new_state=new),
        )

    def test_checks_in_when_caller_goes_away(self):
        self.user("speaking")
        self.reply.assert_not_called()
        self.user("away")
        self.reply.assert_called_once_with(instructions=CHECK_IN)

    def test_running_backend_response_rearms_without_speaking(self):
        self.backend.record(backend_event("response.created"))
        self.user("away")
        self.reset.assert_called_once_with()
        self.reply.assert_not_called()


class BackendResponsesTests(unittest.TestCase):
    def test_tracks_each_delegation_until_it_finishes(self):
        backend = BackendResponses()
        backend.record(backend_event("response.created", "d1"))
        backend.record(backend_event("response.created", "d2"))
        backend.record(backend_event("response.completed", "d1"))
        self.assertEqual(backend.running, {"d2"})
        backend.record(backend_event("response.failed", "d2"))
        self.assertEqual(backend.running, set())

    def test_new_connection_drops_responses_from_the_old_one(self):
        backend = BackendResponses()
        backend.record(backend_event("response.created"))
        backend.record({"type": "session.started"})
        self.assertEqual(backend.running, set())

    def test_model_feeds_server_events_from_its_live_session(self):
        with patch.object(GPTLiveSession, "__init__", lambda self, model: None):
            model = ObservedGPTLiveModel(api_key="offline")
            with patch.object(GPTLiveSession, "on") as on:
                model.session()
        on.assert_any_call("openai_server_event_received", model.backend.record)


if __name__ == "__main__":
    unittest.main()
