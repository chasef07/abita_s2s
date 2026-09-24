"""Replay the provider protocol to verify the missing silence diagnostics."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from livekit.agents.telemetry import gen_ai
from livekit.agents.telemetry.pii import filter_attributes
from livekit.plugins.openai.realtime import GPTLiveSession
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from abita_s2s.runtime.observability import (
    ObservedGPTLiveModel,
    OpenAITimeline,
)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test")
        self.patch = patch("abita_s2s.runtime.observability.tracer", self.tracer)
        self.patch.start()
        self.capture = gen_ai.capture_content_enabled()
        gen_ai.set_capture_content(True)
        with self.tracer.start_as_current_span("agent_session"):
            self.timeline = OpenAITimeline("sip-test")
        self.timeline.record(
            "received", {"type": "session.started", "session": {"id": "live_first"}}
        )

    def tearDown(self):
        self.timeline.close("test_cleanup")
        gen_ai.set_capture_content(self.capture)
        self.patch.stop()
        self.provider.shutdown()

    def backend(self, kind, delegation="d1", **fields):
        self.timeline.record(
            "received",
            {
                "type": "response.event",
                "delegation_id": delegation,
                "event": {"type": kind, **fields},
            },
        )

    def spans(self, name):
        return [s for s in self.exporter.get_finished_spans() if s.name == name]

    def test_complete_delegation_tool_continuation_and_speech_path(self):
        self.timeline.record(
            "received",
            {
                "type": "session.delegation.created",
                "offset_ms": 2000,
                "delegation": {"id": "d1", "target": "responses"},
            },
        )
        self.backend(
            "response.created", response={"id": "resp_one", "model": "gpt-6-luna"}
        )
        self.backend(
            "response.output_item.done",
            item={
                "id": "fc1",
                "type": "function_call",
                "status": "completed",
                "call_id": "tool1",
                "name": "lookup",
                "arguments": '{"name":"PRIVATE"}',
            },
        )
        self.backend(
            "response.completed",
            response={
                "id": "resp_one",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )
        self.timeline.record(
            "queued",
            {
                "type": "response.item.create",
                "event_id": "result1",
                "item": {
                    "type": "function_call_output",
                    "call_id": "tool1",
                    "output": "PRIVATE result",
                },
            },
        )
        self.timeline.record(
            "queued", {"type": "response.create", "event_id": "continue1"}
        )
        self.backend("response.created", response={"id": "resp_two"})
        self.backend(
            "response.output_item.done",
            item={
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "PRIVATE answer"}],
            },
        )
        self.backend("response.completed", response={"id": "resp_two"})
        self.timeline.record(
            "received",
            {
                "type": "session.output_transcript.delta",
                "start_ms": 5000,
                "end_ms": 6000,
                "delta": "PRIVATE speech",
            },
        )
        self.timeline.record(
            "received", {"type": "session.closed", "reason": "close_requested"}
        )
        responses = self.spans("openai.backend_response")
        self.assertEqual(
            [s.attributes["gen_ai.response.id"] for s in responses],
            ["resp_one", "resp_two"],
        )
        self.assertEqual(responses[0].attributes["openai.usage.input_tokens"], 12)
        parent = self.spans("agent_session")[0]
        self.assertTrue(
            all(s.context.trace_id == parent.context.trace_id for s in responses)
        )
        items = self.spans("openai.received.response.output_item.done")
        self.assertEqual(items[1].parent.span_id, responses[1].context.span_id)
        self.assertIn("PRIVATE answer", items[1].attributes["gen_ai.output.messages"])
        queued = self.spans("openai.queued.response.item.create")[0]
        self.assertEqual(queued.attributes["openai.item.call_id"], "tool1")
        self.assertEqual(queued.attributes["openai.direction"], "queued")
        self.assertFalse(self.timeline.responses)

    def test_stalled_response_is_visible_until_disposal_and_overlapping_work_is_separate(
        self,
    ):
        self.backend("response.created", response={"id": "stalled"})
        self.backend("response.created", delegation="d2", response={"id": "done"})
        self.backend("response.completed", delegation="d2", response={"id": "done"})
        self.timeline.close("session_disposed")
        spans = self.spans("openai.backend_response")
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[1].attributes["gen_ai.response.id"], "stalled")
        self.assertEqual(spans[1].attributes["openai.end_reason"], "session_disposed")
        self.assertEqual(spans[1].status.status_code.name, "ERROR")

    def test_reconnect_failure_receipts_and_replacement(self):
        self.backend("response.created", response={"id": "old"})
        self.timeline.record(
            "received", {"type": "session.started", "session": {"id": "new_session"}}
        )
        self.backend("response.created", response={"id": "new"})
        self.backend(
            "response.failed",
            response={
                "id": "new",
                "error": {"code": "server_error", "message": "PRIVATE"},
            },
        )
        self.timeline.record(
            "queued",
            {
                "type": "session.commentary.append",
                "event_id": "append1",
                "delegation_id": "d1",
                "content": "PRIVATE",
            },
        )
        self.timeline.record(
            "received",
            {
                "type": "session.commentary.appended",
                "client_event_id": "append1",
                "offset_ms": 1200,
            },
        )
        spans = self.spans("openai.backend_response")
        self.assertEqual(
            [s.attributes["openai.end_reason"] for s in spans],
            ["connection_replaced", "response.failed"],
        )
        receipt = self.spans("openai.received.session.commentary.appended")[0]
        self.assertEqual(receipt.attributes["openai.client_event_id"], "append1")
        self.assertEqual(receipt.attributes["openai_session_id"], "new_session")

    def test_content_disabled_audio_and_reasoning_never_leak(self):
        gen_ai.set_capture_content(False)
        self.backend("response.created", response={"id": "r"})
        self.backend(
            "response.output_item.done",
            item={
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "PRIVATE"}],
            },
        )
        self.backend("response.reasoning_text.delta", delta="PRIVATE REASONING")
        self.timeline.record(
            "queued", {"type": "session.start", "session": {"instructions": "PRIVATE"}}
        )
        self.timeline.record(
            "queued",
            {
                "type": "response.item.create",
                "item": {"type": "function_call_output", "output": "PRIVATE"},
            },
        )
        self.timeline.record(
            "received",
            {
                "type": "session.input_transcript.delta",
                "delta": "PRIVATE",
                "start_ms": 1,
                "end_ms": 2,
            },
        )
        for _ in range(100):
            self.timeline.record(
                "received",
                {"type": "session.output_audio.delta", "delta": "PRIVATE AUDIO"},
            )
        self.timeline.close("closed")
        serialized = json.dumps(
            [dict(s.attributes) for s in self.exporter.get_finished_spans()]
        )
        self.assertNotIn("PRIVATE", serialized)
        self.assertEqual(len(self.spans("openai.audio_activity")), 1)
        self.assertEqual(
            self.spans("openai.audio_activity")[0].attributes["openai.frame_count"], 100
        )

    def test_livekit_pii_filter_removes_all_captured_content(self):
        self.backend("response.created", response={"id": "public-id"})
        self.backend(
            "response.output_item.done",
            item={
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "PRIVATE"}],
            },
        )
        self.timeline.record(
            "queued",
            {
                "type": "response.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": "tool1",
                    "output": "PRIVATE",
                },
            },
        )
        self.timeline.record(
            "received", {"type": "session.input_transcript.delta", "delta": "PRIVATE"}
        )
        redacted = [
            filter_attributes(s.attributes) for s in self.exporter.get_finished_spans()
        ]
        self.assertNotIn("PRIVATE", json.dumps(redacted))
        self.assertIn("tool1", json.dumps(redacted))

    def test_bad_telemetry_payload_does_not_escape_into_protocol_dispatch(self):
        with self.assertLogs("abita_s2s.runtime.observability", level="ERROR") as logs:
            self.timeline.record(
                "received", {"type": "response.event", "event": "PRIVATE"}
            )
        self.assertNotIn("PRIVATE", str(logs.output))
        self.backend("response.created", response={"id": "still-working"})
        self.backend("response.completed", response={"id": "still-working"})
        self.assertEqual(len(self.spans("openai.backend_response")), 1)

    def test_incomplete_message_and_reasoning_items_are_not_captured(self):
        self.backend("response.created", response={"id": "r"})
        for status, kind in (("incomplete", "message"), ("completed", "reasoning")):
            self.backend(
                "response.output_item.done",
                item={
                    "type": kind,
                    "status": status,
                    "content": [{"type": "output_text", "text": "PRIVATE"}],
                },
            )
        self.assertNotIn(
            "PRIVATE",
            str([dict(s.attributes) for s in self.exporter.get_finished_spans()]),
        )


class StartupObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_session_observes_initial_connection_and_closes_pending_spans(
        self,
    ):
        async def connection(session):
            session.emit("openai_client_event_queued", {"type": "session.start"})
            session.emit(
                "openai_server_event_received",
                {"type": "session.started", "session": {"id": "initial"}},
            )

        with (
            patch.object(GPTLiveSession, "_main_task", connection),
            patch.object(GPTLiveSession, "aclose", new_callable=AsyncMock),
        ):
            model = ObservedGPTLiveModel(api_key="offline", call_id="call1")
            session = model.session()
            await session._main_atask
            self.assertEqual(session.timeline.session_id, "initial")
            with patch.object(session.timeline, "close") as close:
                await session.aclose()
                close.assert_called_once_with("session_disposed")
