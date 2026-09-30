import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.runtime.observability import ObservedGPTLiveModel
from abita_s2s.runtime.silence import CHECK_INS, GOODBYE, SilenceCheckIn


class Handle:
    def __init__(self):
        self.interrupted = False
        self.callbacks = []

    def add_done_callback(self, fn):
        self.callbacks.append(fn)

    def finish(self):
        for fn in self.callbacks:
            fn(self)


class Session:
    def __init__(self):
        self.handlers = {}
        self.reset_away_timer = Mock()
        self.handles = []
        self.generate_reply = Mock(side_effect=self.reply)
        self.shutdown = Mock()

    def reply(self, **_):
        self.handles.append(Handle())
        return self.handles[-1]

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
        self.model = SimpleNamespace(backend_busy=False)
        self.control = SimpleNamespace(closing=False, begin_end=Mock(return_value=None))
        SilenceCheckIn(self.session, self.model, self.control)

    def go_silent(self, windows):
        for _ in range(windows):
            self.session.user("listening", "away")

    def test_checks_in_twice_then_hangs_up_after_one_goodbye(self):
        self.go_silent(5)
        self.assertEqual(self.session.replies(), [*CHECK_INS, GOODBYE])
        self.assertEqual(self.session.reset_away_timer.call_count, 3)
        self.session.shutdown.assert_not_called()
        self.session.handles[-1].finish()
        self.control.begin_end.assert_called_once()
        self.session.shutdown.assert_called_once_with()

    def test_caller_speech_during_goodbye_cancels_hang_up(self):
        self.go_silent(3)
        self.session.user("away", "speaking")
        self.session.handles[-1].finish()
        self.session.shutdown.assert_not_called()

    def test_interrupted_goodbye_does_not_hang_up(self):
        self.go_silent(3)
        self.session.handles[-1].interrupted = True
        self.session.handles[-1].finish()
        self.control.begin_end.assert_not_called()
        self.session.shutdown.assert_not_called()

    def test_blocked_completion_keeps_the_call_open(self):
        self.control.begin_end.return_value = "unavailable: No active SIP call."
        self.go_silent(3)
        self.session.handles[-1].finish()
        self.session.shutdown.assert_not_called()

    def test_caller_speech_restarts_check_ins(self):
        self.go_silent(2)
        self.session.user("away", "speaking")
        self.session.user("speaking", "listening")
        self.go_silent(1)
        self.assertEqual(self.session.replies(), [*CHECK_INS, CHECK_INS[0]])

    def test_running_backend_response_rearms_without_speaking(self):
        self.model.backend_busy = True
        self.go_silent(1)
        self.session.reset_away_timer.assert_called_once()
        self.session.generate_reply.assert_not_called()
        self.model.backend_busy = False
        self.go_silent(1)
        self.assertEqual(self.session.replies(), [CHECK_INS[0]])

    def test_transfer_or_completion_suppresses_check_ins(self):
        self.control.closing = True
        self.go_silent(1)
        self.session.reset_away_timer.assert_not_called()
        self.session.generate_reply.assert_not_called()

    def test_non_away_states_do_nothing(self):
        self.session.user("listening", "speaking")
        self.session.user("speaking", "listening")
        self.session.reset_away_timer.assert_not_called()
        self.session.generate_reply.assert_not_called()


class RealSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_session_wraps_the_model_and_still_checks_in(self):
        model = ObservedGPTLiveModel(api_key="offline")
        session = AgentSession(llm=model)
        self.assertIsNot(session.llm, model)
        SilenceCheckIn(session, model, SimpleNamespace(closing=False))
        with (
            patch.object(session, "reset_away_timer"),
            patch.object(session, "generate_reply") as reply,
        ):
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="listening", new_state="away"),
            )
        reply.assert_called_once_with(instructions=CHECK_INS[0])


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
