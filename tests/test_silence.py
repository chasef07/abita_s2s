import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from livekit.agents import UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.runtime.observability import ObservedGPTLiveModel
from abita_s2s.runtime.silence import CHECK_IN, GOODBYE, SilenceCheckIn


class Session:
    def __init__(self):
        self.handlers = {}
        self.llm = SimpleNamespace(backend_busy=False)
        self.reset_away_timer = Mock()
        self.generate_reply = Mock()

    def on(self, name, fn):
        self.handlers[name] = fn

    def user(self, old, new):
        self.handlers["user_state_changed"](
            UserStateChangedEvent(old_state=old, new_state=new)
        )

    def replies(self):
        return [c.kwargs["instructions"] for c in self.generate_reply.call_args_list]


class SilenceCheckInTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.control = SimpleNamespace(closing=False)
        SilenceCheckIn(self.session, self.control)

    def test_checks_in_twice_then_says_goodbye(self):
        for _ in range(3):
            self.session.user("listening", "away")
        self.assertEqual(self.session.replies(), [CHECK_IN, CHECK_IN, GOODBYE])
        self.assertEqual(self.session.reset_away_timer.call_count, 3)

    def test_caller_speech_restarts_check_ins(self):
        self.session.user("listening", "away")
        self.session.user("away", "listening")
        self.session.user("listening", "away")
        self.session.user("away", "speaking")
        self.session.user("speaking", "listening")
        self.session.user("listening", "away")
        self.assertEqual(self.session.replies(), [CHECK_IN, CHECK_IN, CHECK_IN])

    def test_running_backend_response_rearms_without_speaking(self):
        self.session.llm.backend_busy = True
        self.session.user("listening", "away")
        self.session.reset_away_timer.assert_called_once()
        self.session.generate_reply.assert_not_called()
        self.session.llm.backend_busy = False
        self.session.user("listening", "away")
        self.assertEqual(self.session.replies(), [CHECK_IN])

    def test_transfer_or_completion_suppresses_check_ins(self):
        self.control.closing = True
        self.session.user("listening", "away")
        self.session.reset_away_timer.assert_not_called()
        self.session.generate_reply.assert_not_called()

    def test_non_away_states_do_nothing(self):
        self.session.user("listening", "speaking")
        self.session.user("speaking", "listening")
        self.session.reset_away_timer.assert_not_called()
        self.session.generate_reply.assert_not_called()


class BackendBusyTests(unittest.TestCase):
    def test_tracks_delegated_response_until_completion(self):
        with patch.object(GPTLiveSession, "__init__", lambda self, model: None):
            model = ObservedGPTLiveModel(api_key="offline", call_id="call1")
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
