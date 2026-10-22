"""Trace the observable GPT-Live protocol without changing its execution.

A port of livekit/agents#7408 onto the released plugin: the same spans, attributes,
and session hooks. Delete ProtocolTrace, BackendResponseTrace, and the tracing
overrides once a LiveKit release ships that change.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from livekit.agents import APIConnectionError, llm
from livekit.agents.telemetry import gen_ai, tracer
from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession
from livekit.plugins.openai.realtime.gpt_live_model import DEFAULT_BACKEND_MODEL
from opentelemetry import trace
from opentelemetry.util.types import AttributeValue

from abita_s2s.runtime.delegation_watchdog import DelegationWatchdog
from abita_s2s.runtime.silence import FINISHED, BackendResponses

logger = logging.getLogger(__name__)

TRANSCRIPT_DELTAS = (
    "session.input_transcript.delta",
    "session.output_transcript.delta",
)


@dataclass
class SpeechSegment:
    span: trace.Span
    text: str = ""
    end_ms: int | None = None
    fragments: int = 0
    last_ns: int = 0


class ProtocolTrace:
    """Record transport boundaries separately from inference and actual playback.

    Speech is one span per transcript segment. Output audio keeps one bounded counter, not
    payloads or per-frame spans; input audio flows through silence, so it is not traced.
    ``sent`` means the local WebSocket send completed, not that the service accepted it.
    """

    def __init__(self) -> None:
        self._context = trace.set_span_in_context(trace.get_current_span())
        self._session_id = ""
        self._output_audio: tuple[int, int, int] | None = None
        self._speech: dict[str, SpeechSegment] = {}

    def record(
        self,
        direction: Literal["sent", "received", "send_failed"],
        event: dict[str, Any],
    ) -> None:
        self._guard(self._record, direction, event)

    def speech(
        self,
        segment_id: str,
        role: str,
        text: str,
        start_ms: int | None,
        end_ms: int | None,
    ) -> None:
        """Grow one speaker's segment to its text so far, opening its span on the first fragment."""
        self._guard(self._grow_speech, segment_id, role, text, start_ms, end_ms)

    def end_speech(self, segment_id: str) -> None:
        self._guard(self._end_speech, segment_id)

    def close(self) -> None:
        self._guard(self._close)
        self._output_audio = None
        self._speech.clear()
        self._session_id = ""

    def _guard(self, fn: Callable[..., None], *args: Any) -> None:
        """Never let exporter or payload errors change protocol execution or log content."""
        try:
            fn(*args)
        except Exception as error:
            logger.warning(
                "gpt-live protocol tracing failed",
                extra={"error_type": type(error).__name__},
            )

    def _record(self, direction: str, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.input_audio.append" and direction == "sent":
            return
        if kind == "session.output_audio.delta":
            now = time.time_ns()
            count, start, end = self._output_audio or (0, now, now)
            if now - start >= 1_000_000_000:
                self._flush_audio(count, start, end)
                count, start = 0, now
            self._output_audio = (count + 1, start, now)
            return
        if kind in TRANSCRIPT_DELTAS:
            return
        if kind == "session.started":
            self._session_id = event.get("session", {}).get("id", "")
        inner = event.get("event", {}) if kind == "response.event" else event
        inner_kind = inner.get("type", kind)
        if kind == "response.event" and inner_kind.endswith(".delta"):
            return
        response = inner.get("response")
        if not isinstance(response, dict):
            response = {}
        attrs: dict[str, AttributeValue] = {
            "lk.session_id": self._session_id,
            "lk.openai.direction": direction,
            "lk.openai.event_type": inner_kind,
        }
        for key in (
            "event_id",
            "client_event_id",
            "delegation_id",
            "offset_ms",
            "start_ms",
            "end_ms",
            "sequence_number",
            "reason",
            "code",
            "param",
        ):
            value = event.get(key, inner.get(key))
            if isinstance(value, (str, int, float, bool)):
                attrs[f"lk.openai.{key}"] = value
        for group, source, keys in (
            ("delegation", event.get("delegation"), ("id", "target", "response_id")),
            ("response", response, ("id", "model", "status")),
            ("response.error", response.get("error"), ("code",)),
            (
                "response.incomplete_details",
                response.get("incomplete_details"),
                ("reason",),
            ),
            ("item", inner.get("item"), ("id", "type", "status", "call_id", "name")),
            ("error", event.get("error"), ("type", "code", "param", "client_event_id")),
            ("usage", event.get("usage"), ("seconds",)),
            ("context_window", event.get("context_window"), ("usage_ratio",)),
        ):
            if isinstance(source, dict):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        attrs[f"lk.openai.{group}.{key}"] = value
        with tracer.start_as_current_span(
            "gpt_live.protocol", context=self._context, attributes=attrs
        ) as span:
            if direction == "send_failed" or inner_kind in (
                "error",
                "response.failed",
                "response.incomplete",
            ):
                span.set_status(trace.StatusCode.ERROR)
            if gen_ai.capture_content_enabled():
                content: dict[str, Any] = {}
                if kind in (
                    "session.instructions.append",
                    "session.thinking.append",
                    "session.commentary.append",
                ):
                    content["content"] = event.get("content")
                elif kind in ("session.start", "session.update"):
                    config = event.get("session") or {}
                    content = {
                        key: config[key]
                        for key in ("instructions", "input", "delegation")
                        if key in config
                    }
                elif kind == "response.item.create":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call_output":
                        content["output"] = item.get("output")
                if content:
                    span.set_attribute("lk.pii.openai_content", json.dumps(content))

    def _grow_speech(
        self,
        segment_id: str,
        role: str,
        text: str,
        start_ms: int | None,
        end_ms: int | None,
    ) -> None:
        now = time.time_ns()
        if (segment := self._speech.get(segment_id)) is None:
            attrs: dict[str, AttributeValue] = {
                "lk.session_id": self._session_id,
                "lk.openai.role": role,
            }
            if start_ms is not None:
                attrs["lk.openai.start_ms"] = start_ms
            span = tracer.start_span(
                "gpt_live.speech",
                context=self._context,
                start_time=now,
                attributes=attrs,
            )
            segment = self._speech[segment_id] = SpeechSegment(span)
        segment.text, segment.end_ms, segment.last_ns = text, end_ms, now
        segment.fragments += 1

    def _end_speech(self, segment_id: str) -> None:
        """A segment closes on the next pause or turn; it spans first to last fragment."""
        if (segment := self._speech.pop(segment_id, None)) is None:
            return
        if segment.end_ms is not None:
            segment.span.set_attribute("lk.openai.end_ms", segment.end_ms)
        segment.span.set_attribute("lk.openai.fragment_count", segment.fragments)
        if gen_ai.capture_content_enabled():
            segment.span.set_attribute(
                "lk.pii.openai_content", json.dumps({"transcript": segment.text})
            )
        segment.span.end(end_time=segment.last_ns)

    def _flush_audio(self, count: int, start: int, end: int) -> None:
        span = tracer.start_span(
            "gpt_live.audio_activity",
            context=self._context,
            start_time=start,
            attributes={
                "lk.session_id": self._session_id,
                "lk.openai.event_type": "session.output_audio.delta",
                "lk.openai.direction": "received",
                "lk.openai.frame_count": count,
            },
        )
        span.end(end_time=end)

    def _close(self) -> None:
        if self._session_id:
            self._record(
                "received", {"type": "connection.closed", "reason": "adapter_cleanup"}
            )
        if self._output_audio:
            self._flush_audio(*self._output_audio)
        for segment_id in list(self._speech):
            self._end_speech(segment_id)


@dataclass
class BackendResponseTrace:
    """Completed output items for one managed backend response."""

    span: trace.Span
    record_content: bool
    output: list[dict[str, Any]] = field(default_factory=list)
    has_tool_calls: bool = False

    def finish(self, error: str | None = None) -> None:
        """Export the response once its terminal event arrives or the connection ends."""
        reason = "error" if error else "tool_call" if self.has_tool_calls else "stop"
        gen_ai.set_response_attributes(self.span, finish_reasons=[reason])
        gen_ai.set_content_attributes(self.span, output_messages=self.output)
        if error:
            self.span.set_attribute("error.type", error)
            self.span.set_status(trace.StatusCode.ERROR)
        self.span.end()


class ObservedGPTLiveSession(GPTLiveSession):
    def __init__(self, model: GPTLiveModel):
        super().__init__(model)
        self._backend_traces: dict[str | None, BackendResponseTrace] = {}
        self._protocol_trace = ProtocolTrace()

    @property
    def backend_busy(self) -> bool:
        """Whether a backend response, tool call, or continuation is still outstanding.

        Running responses come from our own tracker, which also clears cancelled ones.
        """
        return bool(
            self._live_model.backend.running
            or self._backend_open_calls
            or self._backend_response_pending
        )

    async def _run_ws(self, ws_conn) -> None:
        try:
            await super()._run_ws(ws_conn)
        finally:
            for response_trace in self._backend_traces.values():
                response_trace.finish("connection_closed")
            self._backend_traces.clear()
            self._protocol_trace.close()

    async def _ws_send(self, ws_conn, event) -> None:
        raw = event if isinstance(event, dict) else event.model_dump(exclude_none=True)
        try:
            await super()._ws_send(ws_conn, raw)
        except APIConnectionError:
            self._protocol_trace.record("send_failed", raw)
            raise
        self._protocol_trace.record("sent", raw)

    def _handle_event(self, event: dict[str, Any]) -> None:
        self._protocol_trace.record("received", event)
        super()._handle_event(event)

    def _handle_transcript_delta(self, role, event) -> None:
        super()._handle_transcript_delta(role, event)
        if event.delta and (speech := self._speech.get(role)) is not None:
            self._protocol_trace.speech(
                speech.message_id, role, speech.text, event.start_ms, speech.end_ms
            )

    def _end_speech(self, role) -> None:
        if (speech := self._speech.get(role)) is not None:
            self._protocol_trace.end_speech(speech.message_id)
        super()._end_speech(role)

    def _trace_response_event(self, envelope) -> None:
        """Trace managed Responses output separately from the speaker conversation.

        The service manages the backend input; the speaker history is not that input.
        """
        event, d_id = envelope.event, envelope.delegation_id
        response = event.response
        if event.type == "response.created":
            if previous := self._backend_traces.pop(d_id, None):
                previous.finish("response_replaced")
            span = tracer.start_span("llm_request", kind=trace.SpanKind.CLIENT)
            record_content = gen_ai.capture_content_enabled() and span.is_recording()
            self._backend_traces[d_id] = BackendResponseTrace(span, record_content)
            gen_ai.set_request_attributes(
                span,
                operation="chat",
                provider=self._live_model.provider,
                model=self._opts.responses.get("model", DEFAULT_BACKEND_MODEL),
                stream=True,
            )
            if d_id is not None:
                span.set_attribute("lk.delegation_id", d_id)
            if self._session_id:
                span.set_attribute("lk.session_id", self._session_id)
            if record_content:
                instructions = self._opts.responses.get("instructions")
                gen_ai.set_content_attributes(
                    span,
                    system_instructions=[{"type": "text", "content": instructions}]
                    if instructions
                    else None,
                    tool_definitions=gen_ai.to_tool_definitions(self._tools.flatten()),
                )
        current = self._backend_traces.get(d_id)
        if current is None:
            return
        if response is not None:
            gen_ai.set_response_attributes(
                current.span, response_id=response.id, model=response.model
            )
            if usage := response.usage:
                gen_ai.set_usage_attributes(
                    current.span,
                    llm.CompletionUsage(
                        prompt_tokens=usage.input_tokens,
                        prompt_cached_tokens=usage.input_tokens_details.cached_tokens,
                        cache_creation_tokens=usage.input_tokens_details.cache_write_tokens,
                        completion_tokens=usage.output_tokens,
                        reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
                        total_tokens=usage.total_tokens,
                    ),
                )
        if (
            event.type == "response.output_item.done"
            and (item := event.item) is not None
            and item.status == "completed"
            and item.type == "message"
            and current.record_content
        ):
            parts = [
                {"type": "text", "content": part.get("text") or part.get("refusal")}
                for part in getattr(item, "content", None) or []
                if part.get("type") in ("output_text", "refusal")
                and (part.get("text") or part.get("refusal"))
            ]
            if parts:
                current.output.append({"role": "assistant", "parts": parts})
        if event.type in FINISHED:
            self._backend_traces.pop(d_id)
            failure = None
            if event.type == "response.failed":
                failure = ((response and response.error) or {}).get(
                    "code"
                ) or event.type
            elif event.type == "response.incomplete":
                details = (response and response.incomplete_details) or {}
                failure = details.get("reason") or event.type
            elif event.type != "response.completed":
                failure = event.type
            current.finish(failure)

    def _handle_response_event(self, envelope) -> None:
        """Tracing never costs the backend a dispatch: a dropped call stalls the delegation.

        Function calls are traced only once dispatch has validated and deduplicated them.
        """
        try:
            self._trace_response_event(envelope)
        except Exception as error:
            logger.warning(
                "gpt-live backend tracing failed",
                extra={"error_type": type(error).__name__},
            )
        accepted: list[llm.FunctionCall] = []
        dispatched = accepted.append
        self.on("function_call", dispatched)
        try:
            super()._handle_response_event(envelope)
        finally:
            self.off("function_call", dispatched)
        if accepted and (current := self._backend_traces.get(envelope.delegation_id)):
            current.has_tool_calls = True
            if current.record_content:
                current.output.extend(
                    gen_ai.to_output_messages(function_calls=accepted)
                )


class ObservedGPTLiveModel(GPTLiveModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.backend = BackendResponses()

    def session(self) -> GPTLiveSession:
        live = ObservedGPTLiveSession(self)
        live.on("openai_server_event_received", self.backend.record)
        live.on("input_speech_started", self.backend.caller_turn)
        self.watchdog = DelegationWatchdog(live, self.backend)
        live.on("openai_server_event_received", self.watchdog.record)
        return live
