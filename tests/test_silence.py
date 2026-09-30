import unittest
from unittest.mock import patch

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.runtime.observability import ObservedGPTLiveModel
from abita_s2s.runtime.silence import CHECK_IN, check_in_on_silence


class SilenceCheckInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = ObservedGPTLiveModel(api_key="offline")
        self.session = AgentSession(llm=self.model)
        check_in_on_silence(self.session, self.model)
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
        with patch.object(ObservedGPTLiveModel, "backend_busy", True):
            self.user("away")
        self.reset.assert_called_once_with()
        self.reply.assert_not_called()


class BackendBusyTests(unittest.TestCase):
    def test_tracks_delegated_response_until_completion(self):
        with patch.object(GPTLiveSession, "__init__", lambda self, model: None):
            model = ObservedGPTLiveModel(api_key="offline")
            self.assertFalse(model.backend_busy)
            with patch.object(GPTLiveSession, "on"):
                live = model.session()
        event = {"type": "response.event", "delegation_id": "d1"}
        live.timeline.record(
            "received", event | {"event": {"type": "response.created"}}
        )
        self.assertTrue(model.backend_busy)
        live.timeline.record(
            "received", event | {"event": {"type": "response.completed"}}
        )
        self.assertFalse(model.backend_busy)


if __name__ == "__main__":
    unittest.main()
