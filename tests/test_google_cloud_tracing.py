"""Collector configuration, export fanout and process/job lifetime boundaries."""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from livekit.agents.telemetry import set_tracer_provider, tracer
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from abita_s2s.runtime.google_cloud_tracing import (
    _CollectorSession,
    register_trace_flush,
    setup_google_cloud_tracing,
)

ENV = {
    "GOOGLE_CLOUD_TRACE_ENDPOINT": "https://collector.example/v1/traces",
    "GOOGLE_CLOUD_TRACE_TOKEN": "private-token",
}


def collector(status, **headers):
    def send(request, **kwargs):
        response = requests.Response()
        response.status_code = status
        response.reason = "PATIENT"
        response.headers.update(headers)
        response._content = b"private-token PATIENT"
        response.request = request
        response.url = request.url
        return response

    return send


def unreachable(request, **kwargs):
    raise requests.RequestException("private-token PATIENT")


class GoogleCloudTracingTests(unittest.TestCase):
    def test_disabled_and_invalid_configuration(self):
        with patch(
            "abita_s2s.runtime.google_cloud_tracing.set_tracer_provider"
        ) as install:
            self.assertIsNone(setup_google_cloud_tracing({}))
            install.assert_not_called()
            for env in (
                {"GOOGLE_CLOUD_TRACE_ENDPOINT": ENV["GOOGLE_CLOUD_TRACE_ENDPOINT"]},
                {"GOOGLE_CLOUD_TRACE_TOKEN": "token"},
                *(
                    {**ENV, "GOOGLE_CLOUD_TRACE_ENDPOINT": url}
                    for url in (
                        "http://collector.example/v1/traces",
                        "https://user:secret@collector.example/v1/traces",
                        "https://collector.example/v1/traces?token=secret",
                        "https://collector.example/v1/traces#secret",
                        "https://collector.example/other",
                    )
                ),
            ):
                with self.subTest(env=env), self.assertRaises(ValueError):
                    setup_google_cloud_tracing(env)
            install.assert_not_called()

    def test_exporter_configuration_and_livekit_fanout(self):
        google = InMemorySpanExporter()
        livekit = InMemorySpanExporter()
        original = tracer._tracer_provider
        with (
            patch(
                "abita_s2s.runtime.google_cloud_tracing.OTLPSpanExporter",
                return_value=google,
            ) as exporter,
            patch(
                "abita_s2s.runtime.google_cloud_tracing.set_tracer_provider",
                wraps=set_tracer_provider,
            ) as install,
        ):
            processor = setup_google_cloud_tracing(
                {**ENV, "LIVEKIT_AGENT_DEPLOYMENT": "staging"}
            )
            provider = install.call_args.args[0]
            try:
                provider.add_span_processor(SimpleSpanProcessor(livekit))
                with tracer.start_as_current_span("gpt_live.protocol") as span:
                    span.set_attribute("lk.session_id", "live_test")
                    span.set_attribute("lk.pii.openai_content", "conversation")
                self.assertTrue(processor.force_flush())
                for sink in (google, livekit):
                    exported = sink.get_finished_spans()[0]
                    self.assertEqual(exported.attributes["lk.session_id"], "live_test")
                    self.assertEqual(
                        exported.resource.attributes["service.name"], "abita-s2s"
                    )
                    self.assertEqual(
                        exported.resource.attributes["deployment.environment.name"],
                        "staging",
                    )
                self.assertEqual(
                    exporter.call_args.kwargs["headers"],
                    {"Authorization": "Bearer private-token"},
                )
                self.assertEqual(exporter.call_args.kwargs["timeout"], 15)
                self.assertTrue(install.call_args.kwargs["allow_pii"])
            finally:
                set_tracer_provider(original)
                provider.shutdown()

    def test_real_otlp_export_serializes_trace_for_authenticated_collector(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        response = requests.Response()
        response.status_code = 200
        original = tracer._tracer_provider
        with (
            patch.object(
                requests.Session, "request", autospec=True, return_value=response
            ) as post,
            patch(
                "abita_s2s.runtime.google_cloud_tracing.set_tracer_provider",
                wraps=set_tracer_provider,
            ) as install,
        ):
            processor = setup_google_cloud_tracing(ENV)
            provider = install.call_args.args[0]
            try:
                with tracer.start_as_current_span("llm_request") as span:
                    span.set_attribute("lk.session_id", "live_test")
                self.assertTrue(processor.force_flush())
                self.assertEqual(
                    post.call_args.kwargs["headers"]["authorization"],
                    "Bearer private-token",
                )
                self.assertEqual(
                    post.call_args.args[1:],
                    ("POST", ENV["GOOGLE_CLOUD_TRACE_ENDPOINT"]),
                )
                payload = ExportTraceServiceRequest.FromString(
                    post.call_args.kwargs["data"]
                )
                exported = payload.resource_spans[0].scope_spans[0].spans[0]
                self.assertEqual(exported.name, "llm_request")
                attrs = {a.key: a.value.string_value for a in exported.attributes}
                self.assertEqual(attrs["lk.session_id"], "live_test")
                self.assertNotIn(b"private-token", post.call_args.kwargs["data"])
            finally:
                set_tracer_provider(original)
                provider.shutdown()

    def test_collector_transport_redacts_failures(self):
        with _CollectorSession() as session:
            for error in (
                requests.ConnectionError,
                requests.Timeout,
                requests.RequestException,
            ):
                with patch.object(
                    requests.Session,
                    "request",
                    side_effect=error("private-token PATIENT"),
                ):
                    with self.assertRaises(error) as caught:
                        session.request("POST", ENV["GOOGLE_CLOUD_TRACE_ENDPOINT"])
                    self.assertNotIn("private-token", str(caught.exception))
                    self.assertNotIn("PATIENT", str(caught.exception))

    def test_exporter_reports_collector_failures_without_token_or_content(self):
        for name, outcome in (
            ("unreachable", unreachable),
            ("rejected", collector(400)),
            ("redirected", collector(302, Location="https://elsewhere.example/")),
        ):
            with self.subTest(name):
                with patch(
                    "abita_s2s.runtime.google_cloud_tracing.set_tracer_provider"
                ) as install:
                    processor = setup_google_cloud_tracing(ENV)
                provider = install.call_args.args[0]
                self.addCleanup(provider.shutdown)
                with (
                    patch.object(
                        requests.adapters.HTTPAdapter, "send", side_effect=outcome
                    ) as send,
                    self.assertLogs("opentelemetry", "WARNING") as logs,
                ):
                    provider.get_tracer("test").start_span("llm_request").end()
                    processor.force_flush()
                self.assertEqual(send.call_count, 1)
                reported = "\n".join(
                    logging.Formatter().format(record) for record in logs.records
                )
                self.assertNotIn("private-token", reported)
                self.assertNotIn("PATIENT", reported)


class TraceLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_flush_preserves_process_provider_for_next_job(self):
        processor = Mock()
        ctx = SimpleNamespace(
            proc=SimpleNamespace(userdata={"google_cloud_trace_processor": processor}),
            add_shutdown_callback=Mock(),
        )
        register_trace_flush(ctx)
        await ctx.add_shutdown_callback.call_args.args[0]()
        processor.force_flush.assert_called_once_with(17000)
        processor.shutdown.assert_not_called()
        processor.force_flush.return_value = False
        with self.assertLogs("abita_s2s.runtime.google_cloud_tracing", level="ERROR"):
            await ctx.add_shutdown_callback.call_args.args[0]()

    async def test_disabled_has_no_flush_callback(self):
        ctx = SimpleNamespace(
            proc=SimpleNamespace(userdata={}), add_shutdown_callback=Mock()
        )
        register_trace_flush(ctx)
        ctx.add_shutdown_callback.assert_not_called()
