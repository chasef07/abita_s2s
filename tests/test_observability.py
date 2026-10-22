"""Replay the GPT-Live protocol through the session to verify the livekit/agents#7408 port."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from livekit.agents import APIConnectionError
from livekit.agents.telemetry import gen_ai
from livekit.agents.telemetry.pii import filter_attributes
from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from abita_s2s.runtime.observability import ObservedGPTLiveSession, ProtocolTrace


def backend(delegation, inner):
    return {"type": "response.event", "delegation_id": delegation, "event": inner}


def message(text):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def function_call(call_id, status="completed", arguments='{"name":"PRIVATE"}'):
    return {
        "type": "response.output_item.done",
        "item": {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "status": status,
            "call_id": call_id,
            "name": "lookup",
            "arguments": arguments,
        },
    }


def completed(response_id):
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "model": "gpt-6-luna",
            "usage": {
                "input_tokens": 376,
                "input_tokens_details": {
                    "cached_tokens": 100,
                    "cache_write_tokens": 20,
                },
                "output_tokens": 18,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 394,
            },
        },
    }


def transcript(role, text, start_ms):
    side = "input" if role == "user" else "output"
    return {
        "type": f"session.{side}_transcript.delta",
        "delta": text,
        "start_ms": start_ms,
        "end_ms": start_ms + 200,
    }


async def idle(session):
    async for _ in session._msg_ch:
        pass


class TraceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test")
        patch("abita_s2s.runtime.observability.tracer", self.tracer).start()
        patch.object(GPTLiveSession, "_main_task", idle).start()
        self.addCleanup(patch.stopall)
        capture = gen_ai.capture_content_enabled()
        self.addCleanup(gen_ai.set_capture_content, capture)
        gen_ai.set_capture_content(True)
        self.model = GPTLiveModel(
            api_key="offline",
            responses_options={"model": "gpt-6-luna", "instructions": "PRIVATE prompt"},
        )
        with self.tracer.start_as_current_span("agent_session") as parent:
            self.parent = parent
            self.session = ObservedGPTLiveSession(self.model)
        self.session._handle_event(
            {"type": "session.started", "session": {"id": "live_test"}}
        )

    async def asyncTearDown(self):
        await self.session.aclose()
        await self.model.aclose()
        self.provider.shutdown()

    def receive(self, *events):
        with trace.use_span(self.parent, end_on_exit=False):
            for event in events:
                self.session._handle_event(event)

    async def end_connection(self):
        async def connection(session, ws_conn):
            return None

        with patch.object(GPTLiveSession, "_run_ws", connection):
            await self.session._run_ws(None)

    def spans(self, name):
        return [s for s in self.exporter.get_finished_spans() if s.name == name]

    def protocol(self):
        return {
            s.attributes["lk.openai.event_type"]: s
            for s in self.spans("gpt_live.protocol")
        }


class BackendResponseTraceTests(TraceCase):
    async def test_text_only_answer_is_one_llm_request_under_the_call(self):
        self.receive(
            backend("d1", {"type": "response.created", "response": {"id": "r1"}}),
            backend("d1", message("Please ask for a city.")),
            backend(
                "d1",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "reasoning",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "PRIVATE thought"}],
                    },
                },
            ),
            backend("d1", completed("r1")),
        )
        (span,) = self.spans("llm_request")
        attrs = dict(span.attributes)
        self.assertEqual(span.parent.span_id, self.parent.get_span_context().span_id)
        self.assertEqual(attrs["lk.delegation_id"], "d1")
        self.assertEqual(attrs["lk.session_id"], "live_test")
        self.assertEqual(attrs["gen_ai.response.id"], "r1")
        self.assertEqual(attrs["gen_ai.request.model"], "gpt-6-luna")
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], 376)
        self.assertEqual(attrs["gen_ai.usage.cache_read.input_tokens"], 100)
        self.assertEqual(attrs["gen_ai.usage.reasoning.output_tokens"], 5)
        self.assertEqual(attrs["gen_ai.response.finish_reasons"], ("stop",))
        self.assertEqual(
            json.loads(attrs["gen_ai.output.messages"]),
            [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "Please ask for a city."}],
                }
            ],
        )
        self.assertIn("PRIVATE prompt", attrs["gen_ai.system_instructions"])
        self.assertNotIn("PRIVATE thought", str(attrs))
        self.assertNotIn("PRIVATE", str(filter_attributes(attrs)))
        self.assertEqual(self.session._history.items, [])

    async def test_delegations_and_continuations_trace_separately_with_accepted_calls(
        self,
    ):
        self.receive(
            backend("d1", {"type": "response.created"}),
            backend("d2", {"type": "response.created"}),
            backend("d1", function_call("c1")),
            backend("d1", function_call("c1")),
            backend("d1", function_call("c2", status="in_progress")),
            backend("d2", message("Second delegation.")),
            backend("d2", completed("r2")),
            backend("d1", completed("r1")),
            backend("d1", {"type": "response.created"}),
            backend("d1", message("Continuation.")),
            backend("d1", completed("r3")),
        )
        spans = {
            s.attributes["gen_ai.response.id"]: dict(s.attributes)
            for s in self.spans("llm_request")
        }
        self.assertEqual(set(spans), {"r1", "r2", "r3"})
        self.assertEqual(spans["r1"]["gen_ai.response.finish_reasons"], ("tool_call",))
        self.assertEqual(
            json.loads(spans["r1"]["gen_ai.output.messages"])[0]["parts"],
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "lookup",
                    "arguments": {"name": "PRIVATE"},
                }
            ],
        )
        self.assertIn("Second delegation.", spans["r2"]["gen_ai.output.messages"])
        self.assertIn("Continuation.", spans["r3"]["gen_ai.output.messages"])
        self.assertNotIn("c1", str(spans["r3"]))
        self.assertEqual(len(self.session._history.items), 1)

    async def test_refusals_are_kept_and_unfinished_messages_are_not(self):
        unfinished = message("Unfinished text")
        unfinished["item"]["status"] = "in_progress"
        refusal = message("")
        refusal["item"]["content"] = [{"type": "refusal", "refusal": "Cannot help."}]
        self.receive(
            backend("d1", {"type": "response.created"}),
            backend("d1", unfinished),
            backend("d1", refusal),
            backend("d1", completed("r1")),
        )
        (span,) = self.spans("llm_request")
        self.assertEqual(
            json.loads(span.attributes["gen_ai.output.messages"]),
            [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "Cannot help."}],
                }
            ],
        )

    async def test_failure_reports_the_provider_code(self):
        for ending, response, error_type in (
            (
                "response.failed",
                {"error": {"code": "server_error", "message": "PROVIDER DETAIL"}},
                "server_error",
            ),
            ("response.failed", {"error": None}, "response.failed"),
            (
                "response.incomplete",
                {"incomplete_details": {"reason": "content_filter"}},
                "content_filter",
            ),
            ("response.incomplete", {}, "response.incomplete"),
        ):
            with self.subTest(ending=ending, error_type=error_type):
                self.exporter.clear()
                self.receive(
                    backend(
                        "d1", {"type": "response.created", "response": {"id": "r1"}}
                    ),
                    backend(
                        "d1", {"type": ending, "response": {"id": "r1", **response}}
                    ),
                )
                (span,) = self.spans("llm_request")
                self.assertEqual(span.attributes["error.type"], error_type)
                self.assertEqual(
                    span.attributes["gen_ai.response.finish_reasons"], ("error",)
                )
                self.assertFalse(span.status.is_ok)
                self.assertNotIn("PROVIDER DETAIL", str(dict(span.attributes)))

    async def test_cancelled_response_finishes_its_trace(self):
        self.receive(
            backend("d1", {"type": "response.created", "response": {"id": "r1"}}),
            backend("d1", {"type": "response.cancelled", "response": {"id": "r1"}}),
        )
        (span,) = self.spans("llm_request")
        self.assertEqual(span.attributes["error.type"], "response.cancelled")
        self.assertFalse(self.session._backend_traces)

    async def test_connection_end_finishes_unfinished_responses_and_speech(self):
        self.receive(
            {
                "type": "response.event",
                "event": {"type": "response.created", "response": {"id": "r1"}},
            },
            transcript("assistant", "PRIVATE half", 0),
        )
        await self.end_connection()
        (span,) = self.spans("llm_request")
        self.assertEqual(span.attributes["error.type"], "connection_closed")
        self.assertFalse(span.status.is_ok)
        self.assertFalse(self.session._backend_traces)
        self.assertEqual(len(self.spans("gpt_live.speech")), 1)
        self.assertEqual(
            self.protocol()["connection.closed"].attributes["lk.openai.reason"],
            "adapter_cleanup",
        )

    async def test_tracing_failure_never_drops_dispatch(self):
        calls = []
        self.session.on("function_call", calls.append)
        with patch.object(
            self.session, "_trace_response_event", side_effect=RuntimeError("PRIVATE")
        ):
            with self.assertLogs("abita_s2s.runtime.observability", "WARNING") as logs:
                self.receive(
                    backend("d1", {"type": "response.created"}),
                    backend("d1", function_call("c1")),
                    backend("d1", completed("r1")),
                )
        self.assertEqual([c.call_id for c in calls], ["c1"])
        self.assertEqual(self.session._backend_open_calls, {"c1"})
        self.assertNotIn("PRIVATE", str(logs.output))

    async def test_content_capture_off_keeps_ids_and_drops_content(self):
        gen_ai.set_capture_content(False)
        self.receive(
            backend("d1", {"type": "response.created", "response": {"id": "r1"}}),
            backend("d1", message("PRIVATE answer")),
            backend("d1", function_call("c1")),
            backend("d1", completed("r1")),
        )
        await self.session._ws_send(
            SimpleNamespace(send_str=AsyncMock()),
            {
                "type": "response.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "PRIVATE",
                },
            },
        )
        self.receive(transcript("user", "PRIVATE words", 0))
        await self.end_connection()
        attrs = [dict(s.attributes) for s in self.exporter.get_finished_spans()]
        self.assertNotIn("PRIVATE", json.dumps(attrs))
        self.assertEqual(
            self.spans("llm_request")[0].attributes["gen_ai.response.id"], "r1"
        )


class ProtocolTraceTests(TraceCase):
    async def test_sent_commands_and_receipts_correlate(self):
        ws = SimpleNamespace(send_str=AsyncMock())
        self.receive(
            {
                "type": "session.delegation.created",
                "offset_ms": 50,
                "delegation": {"id": "d1", "target": "responses"},
            }
        )
        await self.session._ws_send(
            ws,
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
        await self.session._ws_send(
            ws,
            {
                "type": "session.commentary.append",
                "event_id": "context1",
                "delegation_id": "d1",
                "content": "PRIVATE context",
            },
        )
        self.receive(
            {
                "type": "session.commentary.appended",
                "client_event_id": "context1",
                "offset_ms": 100,
            }
        )
        events = [dict(s.attributes) for s in self.spans("gpt_live.protocol")]
        command = [e for e in events if e.get("lk.openai.event_id") == "result1"]
        self.assertEqual([e["lk.openai.direction"] for e in command], ["sent"])
        self.assertEqual(command[0]["lk.openai.item.call_id"], "tool1")
        self.assertEqual(command[0]["lk.session_id"], "live_test")
        self.assertTrue(
            any(e.get("lk.openai.client_event_id") == "context1" for e in events)
        )
        self.assertTrue(any(e.get("lk.openai.delegation.id") == "d1" for e in events))
        self.assertIn("PRIVATE", str(events))
        self.assertNotIn("PRIVATE", str([filter_attributes(e) for e in events]))
        parent = self.parent.get_span_context().trace_id
        self.assertTrue(
            all(s.context.trace_id == parent for s in self.spans("gpt_live.protocol"))
        )

    async def test_send_failure_never_claims_sent(self):
        failing = SimpleNamespace(
            send_str=AsyncMock(side_effect=ConnectionError("PRIVATE"))
        )
        for kind in ("response.create", "session.input_audio.append"):
            with self.subTest(kind=kind), self.assertRaises(APIConnectionError):
                await self.session._ws_send(
                    failing, {"type": kind, "event_id": kind, "delta": "PRIVATE"}
                )
        events = [dict(s.attributes) for s in self.spans("gpt_live.protocol")]
        self.assertEqual(
            [
                (e["lk.openai.event_id"], e["lk.openai.direction"])
                for e in events
                if "lk.openai.event_id" in e
            ],
            [
                ("response.create", "send_failed"),
                ("session.input_audio.append", "send_failed"),
            ],
        )
        self.assertNotIn("PRIVATE", str(events))

    async def test_one_span_per_speech_segment(self):
        self.receive(
            transcript("user", "PRIVATE what is", 0),
            transcript("user", " the weather", 200),
        )
        self.session._end_speech("user")
        self.receive(
            transcript("assistant", "PRIVATE sunny", 1000),
            transcript("assistant", " today", 1200),
            transcript("assistant", "Anything else?", 3000),
        )
        await self.end_connection()
        speech = [dict(s.attributes) for s in self.spans("gpt_live.speech")]
        self.assertEqual(
            [
                (
                    s["lk.openai.role"],
                    s["lk.openai.fragment_count"],
                    s["lk.openai.start_ms"],
                    s["lk.openai.end_ms"],
                    s["lk.session_id"],
                )
                for s in speech
            ],
            [
                ("user", 2, 0, 400, "live_test"),
                ("assistant", 2, 1000, 1400, "live_test"),
                ("assistant", 1, 3000, 3200, "live_test"),
            ],
        )
        self.assertEqual(
            [json.loads(s["lk.pii.openai_content"])["transcript"] for s in speech],
            ["PRIVATE what is the weather", "PRIVATE sunny today", "Anything else?"],
        )
        self.assertFalse(
            [e for e in self.protocol() if e.endswith("_transcript.delta")]
        )
        self.assertNotIn("PRIVATE", str([filter_attributes(s) for s in speech]))

    def test_output_audio_is_bounded_and_input_audio_is_not_traced(self):
        with patch("abita_s2s.runtime.observability.time.time_ns", return_value=100):
            timeline = ProtocolTrace()
            timeline.record(
                "received", {"type": "session.started", "session": {"id": "live_audio"}}
            )
            for _ in range(1000):
                timeline.record(
                    "received",
                    {"type": "session.output_audio.delta", "delta": "PRIVATE"},
                )
                timeline.record(
                    "sent", {"type": "session.input_audio.append", "audio": "PRIVATE"}
                )
            timeline.close()
            timeline.close()
        (audio,) = self.spans("gpt_live.audio_activity")
        self.assertEqual(audio.attributes["lk.openai.frame_count"], 1000)
        self.assertEqual(audio.attributes["lk.session_id"], "live_audio")
        spans = self.exporter.get_finished_spans()
        self.assertFalse(
            [
                s
                for s in spans
                if s.attributes.get("lk.openai.event_type")
                == "session.input_audio.append"
            ]
        )
        self.assertNotIn("PRIVATE", str([dict(s.attributes) for s in spans]))

    def test_keeps_new_lifecycle_events_and_failure_codes(self):
        timeline = ProtocolTrace()
        timeline.record(
            "received", {"type": "session.started", "session": {"id": "live_codes"}}
        )
        for inner in (
            {"type": "response.queued"},
            {"type": "response.web_search_call.searching", "item_id": "ws_1"},
            {
                "type": "response.reasoning_summary_text.done",
                "text": "PRIVATE REASONING",
            },
            {"type": "response.output_text.delta", "delta": "PRIVATE TOKEN"},
            {
                "type": "error",
                "code": "server_error",
                "param": "tools",
                "message": "PRIVATE",
            },
            {
                "type": "response.failed",
                "response": {
                    "id": "r1",
                    "error": {"code": "rate_limit_exceeded", "message": "PRIVATE"},
                },
            },
            {
                "type": "response.incomplete",
                "response": {
                    "id": "r2",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            },
        ):
            timeline.record("received", backend("d1", inner))
        timeline.record(
            "received", {"type": "session.usage.updated", "usage": {"seconds": 12.5}}
        )
        timeline.record(
            "received", {"type": "info", "code": "data_channel_permissions"}
        )
        timeline.record(
            "received",
            {
                "type": "session.closed",
                "reason": "close_requested",
                "usage": {"seconds": 15.0},
            },
        )
        timeline.close()
        events = {k: dict(s.attributes) for k, s in self.protocol().items()}
        self.assertNotIn("response.output_text.delta", events)
        for kind in (
            "response.queued",
            "response.web_search_call.searching",
            "response.reasoning_summary_text.done",
        ):
            self.assertIn(kind, events)
        self.assertEqual(
            (events["error"]["lk.openai.code"], events["error"]["lk.openai.param"]),
            ("server_error", "tools"),
        )
        self.assertFalse(self.protocol()["error"].status.is_ok)
        self.assertEqual(
            events["response.failed"]["lk.openai.response.error.code"],
            "rate_limit_exceeded",
        )
        self.assertEqual(
            events["response.incomplete"][
                "lk.openai.response.incomplete_details.reason"
            ],
            "max_output_tokens",
        )
        self.assertEqual(
            events["session.usage.updated"]["lk.openai.usage.seconds"], 12.5
        )
        self.assertEqual(events["session.closed"]["lk.openai.usage.seconds"], 15.0)
        self.assertEqual(events["info"]["lk.openai.code"], "data_channel_permissions")
        self.assertNotIn("PRIVATE", str(events))

    def test_bad_payload_is_logged_without_content(self):
        with self.assertLogs("abita_s2s.runtime.observability", "WARNING") as logs:
            ProtocolTrace().record(
                "received", {"type": "response.event", "event": "PRIVATE"}
            )
        self.assertNotIn("PRIVATE", str(logs.output))
