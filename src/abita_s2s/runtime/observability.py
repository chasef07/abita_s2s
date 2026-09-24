"""Trace the observable GPT-Live protocol without changing its execution.

Queued commands are not delivery receipts. Managed backend input and the voice
model's internal consumption of results are not exposed by this protocol.
"""

import json
import logging
import time

from livekit.agents.telemetry import gen_ai, tracer
from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession
from opentelemetry import trace

logger = logging.getLogger(__name__)


class OpenAITimeline:
    """One connection timeline; retain only currently running backend spans."""

    def __init__(self, call_id: str):
        self.call_id = call_id
        self.session_id = ""
        self.context = trace.set_span_in_context(trace.get_current_span())
        self.responses: dict[str | None, trace.Span] = {}
        self.audio: dict[str, tuple[int, int, int]] = {}

    def record(self, direction: str, event: dict) -> None:
        # Observability must not interrupt provider event dispatch. Report failures
        # without dumping exception text or raw payloads containing patient data.
        try:
            self._record(direction, event)
        except Exception as error:
            logger.error("openai_timeline_error cause=%s", type(error).__name__)

    def _record(self, direction: str, event: dict) -> None:
        kind = event.get("type", "")
        now = time.time_ns()
        if kind in ("session.input_audio.append", "session.output_audio.delta"):
            # Continuous full-duplex audio includes silence. These are transport
            # activity windows, never evidence that speech was heard.
            count, start, end = self.audio.get(kind, (0, now, now))
            if now - start >= 1_000_000_000:
                self._audio_span(kind, count, start, end)
                count, start = 0, now
            self.audio[kind] = (count + 1, start, now)
            return
        if not (
            kind.startswith("session.")
            or kind
            in ("response.event", "response.create", "response.item.create", "error")
        ):
            return

        if kind == "session.started":
            new_id = event.get("session", {}).get("id", "")
            if self.session_id and new_id != self.session_id:
                self.close("connection_replaced")
            self.session_id = new_id
            logger.info(
                "openai_session_started call_id=%s openai_session_id=%s",
                self.call_id,
                self.session_id,
                extra={"call_id": self.call_id, "openai_session_id": self.session_id},
            )

        inner = event.get("event", {}) if kind == "response.event" else event
        inner_kind = inner.get("type", kind)
        # Token deltas are replaced by completed items; do not capture reasoning.
        if kind == "response.event" and inner_kind not in (
            "response.created",
            "response.in_progress",
            "response.completed",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
            "response.output_item.done",
            "error",
        ):
            return
        attrs = {
            "call_id": self.call_id,
            "openai_session_id": self.session_id,
            "openai.direction": direction,
            "openai.event_type": inner_kind,
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
        ):
            value = event.get(key, inner.get(key))
            if isinstance(value, (str, int, float, bool)):
                attrs[f"openai.{key}"] = value
        delegation = event.get("delegation") or {}
        for key in ("id", "target", "response_id"):
            if delegation.get(key) is not None:
                attrs[f"openai.delegation.{key}"] = delegation[key]
        response = inner.get("response") or {}
        for key in ("id", "model", "status"):
            if response.get(key) is not None:
                attrs[f"openai.response.{key}"] = response[key]
        for group in ("usage", "context_window"):
            for key, value in (event.get(group) or {}).items():
                if isinstance(value, (int, float)):
                    attrs[f"openai.{group}.{key}"] = value
        item = inner.get("item") or {}
        for key in ("id", "type", "status", "call_id", "name"):
            if item.get(key) is not None:
                attrs[f"openai.item.{key}"] = item[key]
        for source in (
            event.get("error"),
            response.get("error"),
            response.get("incomplete_details"),
        ):
            for key in ("type", "code", "param", "client_event_id", "reason"):
                if isinstance(source, dict) and isinstance(source.get(key), str):
                    attrs[f"openai.error.{key}"] = source[key]

        d_id = event.get("delegation_id")
        if inner_kind == "response.created":
            self._finish(d_id, "response_replaced")
            span = tracer.start_span(
                "openai.backend_response", context=self.context, attributes=attrs
            )
            gen_ai.set_request_attributes(
                span,
                operation="chat",
                provider="openai",
                model=response.get("model"),
                stream=True,
            )
            self.responses[d_id] = span
        backend = self.responses.get(d_id) if kind == "response.event" else None
        if backend is not None:
            gen_ai.set_response_attributes(
                backend, response_id=response.get("id"), model=response.get("model")
            )
            usage = response.get("usage") or {}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                if isinstance(usage.get(field), int):
                    backend.set_attribute(f"openai.usage.{field}", usage[field])
        context = (
            trace.set_span_in_context(backend) if backend is not None else self.context
        )
        with tracer.start_as_current_span(
            f"openai.{direction}.{inner_kind}", context=context, attributes=attrs
        ) as span:
            if inner_kind in ("error", "response.failed", "response.incomplete"):
                span.set_status(trace.StatusCode.ERROR)
            if gen_ai.capture_content_enabled():
                content = {}
                if kind.endswith("transcript.delta"):
                    content["delta"] = event.get("delta")
                if kind in (
                    "session.instructions.append",
                    "session.thinking.append",
                    "session.commentary.append",
                ):
                    content["content"] = event.get("content")
                if kind in ("session.start", "session.update"):
                    config = event.get("session") or {}
                    content = {
                        k: config[k]
                        for k in ("instructions", "input", "delegation")
                        if k in config
                    }
                if item.get("type") == "function_call_output":
                    content["output"] = item.get("output")
                if (
                    inner_kind == "response.output_item.done"
                    and item.get("status") == "completed"
                ):
                    if item.get("type") == "message":
                        parts = [
                            {
                                "type": "text",
                                "content": p.get("text") or p.get("refusal"),
                            }
                            for p in item.get("content", [])
                            if p.get("type") in ("output_text", "refusal")
                        ]
                        gen_ai.set_content_attributes(
                            span,
                            output_messages=[{"role": "assistant", "parts": parts}],
                        )
                    elif item.get("type") == "function_call":
                        # This is the provider's proposed call, not proof of dispatch.
                        content["arguments"] = item.get("arguments")
                if content:
                    span.set_attribute("lk.pii.openai_content", json.dumps(content))
        if "transcript.delta" not in kind:
            logger.info("openai_timeline %s %s", direction, inner_kind, extra=attrs)
        if inner_kind in (
            "response.completed",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        ):
            self._finish(d_id, inner_kind)
        if kind == "session.closed":
            self.close("session_closed")

    def _finish(self, delegation_id: str | None, reason: str) -> None:
        span = self.responses.pop(delegation_id, None)
        if span is not None:
            span.set_attribute("openai.end_reason", reason)
            if reason != "response.completed":
                span.set_status(trace.StatusCode.ERROR)
            span.end()

    def _audio_span(self, kind: str, count: int, start: int, end: int) -> None:
        span = tracer.start_span(
            "openai.audio_activity",
            context=self.context,
            start_time=start,
            attributes={
                "call_id": self.call_id,
                "openai_session_id": self.session_id,
                "openai.event_type": kind,
                "openai.frame_count": count,
                "openai.direction": "queued"
                if kind == "session.input_audio.append"
                else "received",
            },
        )
        span.end(end_time=end)

    def close(self, reason: str) -> None:
        for d_id in list(self.responses):
            self._finish(d_id, reason)
        for kind, (count, start, end) in self.audio.items():
            self._audio_span(kind, count, start, end)
        self.audio.clear()


class ObservedGPTLiveSession(GPTLiveSession):
    def __init__(self, model: GPTLiveModel, call_id: str):
        super().__init__(model)
        # GPTLiveSession schedules connection work; attach synchronously before
        # yielding so the initial session.start and session.started are observed.
        self.timeline = OpenAITimeline(call_id)
        self.on(
            "openai_server_event_received",
            lambda event: self.timeline.record("received", event),
        )
        self.on(
            "openai_client_event_queued",
            lambda event: self.timeline.record("queued", event),
        )

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            self.timeline.close("session_disposed")


class ObservedGPTLiveModel(GPTLiveModel):
    def __init__(self, *, call_id: str = "", **kwargs):
        super().__init__(**kwargs)
        self.call_id = call_id

    def session(self) -> GPTLiveSession:
        return ObservedGPTLiveSession(self, self.call_id)
