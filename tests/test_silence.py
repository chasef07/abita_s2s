import unittest
from types import SimpleNamespace
from unittest.mock import patch

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession

from abita_s2s.runtime.silence import (
    CHECK_IN,
    COULD_NOT_FINISH,
    STILL_WORKING,
    BackendResponses,
    check_in_on_silence,
)


async def idle(session):
    async for _ in session._msg_ch:
        pass


def backend_event(kind, delegation_id="d1"):
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": {"type": kind},
    }


class SilenceCheckInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = BackendResponses()
        self.session = AgentSession(llm=GPTLiveModel(api_key="offline"))
        self.control = SimpleNamespace(leaving=False)
        self.handed_off = False
        check_in_on_silence(
            self.session, self.backend, self.control, lambda: self.handed_off
        )
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

    def test_unanswered_caller_words_go_to_the_backend_instead_of_a_check_in(self):
        self.handed_off = True
        self.user("away")
        self.reply.assert_not_called()
        self.reset.assert_called_once_with()

    def test_running_backend_response_tells_caller_it_is_still_working(self):
        self.backend.record(backend_event("response.created"))
        self.user("away")
        self.reply.assert_called_once_with(instructions=STILL_WORKING)
        self.reset.assert_called_once_with()

    def test_failed_backend_response_tells_caller(self):
        for kind in ("response.failed", "response.incomplete"):
            with self.subTest(kind=kind):
                self.reply.reset_mock()
                self.backend.record(backend_event("response.created"))
                self.backend.record(backend_event(kind))
                self.reply.assert_called_once_with(instructions=COULD_NOT_FINISH)

    def test_completed_or_cancelled_backend_response_stays_quiet(self):
        for kind in ("response.completed", "response.cancelled"):
            self.backend.record(backend_event("response.created"))
            self.backend.record(backend_event(kind))
        self.reply.assert_not_called()

    def test_failure_stays_quiet_for_watchdog_other_work_or_a_leaving_call(self):
        self.backend.record(backend_event("response.created", None))
        self.backend.record(backend_event("response.failed", None))
        self.backend.record(backend_event("response.created", "d1"))
        self.backend.record(backend_event("response.created", "d2"))
        self.backend.record(backend_event("response.failed", "d1"))
        self.control.leaving = True
        self.backend.record(backend_event("response.failed", "d2"))
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

    def test_caller_turn_counts_until_the_next_request_not_a_continuation(self):
        backend = BackendResponses()
        backend.record({"type": "session.delegation.created"})
        backend.record(backend_event("response.created", "d1"))
        self.assertFalse(backend.caller_spoke_since_request)
        backend.caller_turn()
        backend.record(backend_event("response.completed", "d1"))
        backend.record(backend_event("response.created", "d1"))
        self.assertTrue(backend.caller_spoke_since_request)
        backend.record({"type": "session.delegation.created"})
        self.assertTrue(backend.caller_spoke_since_request)
        backend.record(backend_event("response.completed", "d1"))
        backend.record({"type": "session.delegation.created"})
        self.assertFalse(backend.caller_spoke_since_request)
        backend.caller_turn()
        backend.request_started()
        self.assertFalse(backend.caller_spoke_since_request)

    def test_new_connection_drops_responses_from_the_old_one(self):
        backend = BackendResponses()
        backend.record(backend_event("response.created"))
        backend.record({"type": "session.started"})
        self.assertEqual(backend.running, set())


class BackendBusyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(GPTLiveSession, "_main_task", idle):
            model = GPTLiveModel(api_key="offline")
            self.live = GPTLiveSession(model)
        self.addAsyncCleanup(model.aclose)
        self.addAsyncCleanup(self.live.aclose)
        self.backend = BackendResponses()
        self.backend.follow(self.live)

    def receive(self, event):
        self.live.emit("openai_server_event_received", event)
        self.live._handle_event(event)

    def test_unanswered_tool_calls_keep_the_backend_busy_after_the_response(self):
        self.assertFalse(self.backend.busy)
        self.receive(backend_event("response.created"))
        self.assertEqual((self.backend.running, self.backend.busy), ({"d1"}, True))
        self.receive(
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "c1",
                        "name": "lookup",
                        "arguments": "{}",
                    },
                },
            }
        )
        self.receive(backend_event("response.completed"))
        self.assertEqual((self.backend.running, self.backend.busy), (set(), True))

    def test_cancelled_response_frees_the_backend(self):
        self.receive(backend_event("response.created"))
        self.receive(backend_event("response.cancelled"))
        self.assertFalse(self.backend.busy)

    def test_caller_speech_counts_as_a_turn(self):
        self.backend.request_started()
        self.receive({"type": "session.input_transcript.delta", "delta": " Hi"})
        self.assertTrue(self.backend.caller_spoke_since_request)


if __name__ == "__main__":
    unittest.main()
