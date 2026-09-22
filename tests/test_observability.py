"""Provider session IDs remain correlated across startup and reconnects."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from abita_s2s.observability import log_openai_session


class ObservabilityTests(unittest.TestCase):
    def test_openai_session_id_before_or_after_startup_and_on_reconnect(self):
        for initial_id in (None, "live_first"):
            with self.subTest(initial_id=initial_id):
                session = SimpleNamespace(session_id=initial_id, on=Mock())
                with self.assertLogs("abita_s2s.observability", level="INFO") as logs:
                    log_openai_session(session, "sip-test")
                    event_name, listener = session.on.call_args.args
                    self.assertEqual(event_name, "openai_server_event_received")
                    if initial_id is None:
                        listener(
                            {"type": "session.started", "session": {"id": "live_first"}}
                        )
                    listener({"type": "output_audio.delta", "delta": "private-audio"})
                    listener(
                        {"type": "session.started", "session": {"id": "live_reconnect"}}
                    )
                self.assertEqual(
                    [record.openai_session_id for record in logs.records],
                    ["live_first", "live_reconnect"],
                )
                self.assertTrue(
                    all(record.call_id == "sip-test" for record in logs.records)
                )
