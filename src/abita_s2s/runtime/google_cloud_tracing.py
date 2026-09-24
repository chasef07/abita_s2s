"""Export call traces to the same authenticated collector as abita_agent."""

import asyncio
import logging
import os
from urllib.parse import urlsplit

import requests
from livekit.agents.telemetry import set_tracer_provider
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


class _CollectorSession(requests.Session):
    """Keep exporter diagnostics free of credentials and returned patient content."""

    def post(self, url, **kwargs):
        try:
            response = super().post(url, **kwargs, allow_redirects=False)
        except requests.ConnectionError:
            raise requests.ConnectionError(
                "Trace collector connection failed"
            ) from None
        except requests.Timeout:
            raise requests.Timeout("Trace collector request timed out") from None
        except requests.RequestException:
            raise requests.RequestException(
                "Trace collector transport failed"
            ) from None
        if 300 <= response.status_code < 400:
            response.status_code = 400  # A redirect is not accepted telemetry.
        response.reason = "Trace collector response"
        response._content = (
            b""  # The exporter needs status only; never log response bodies.
        )
        return response


def setup_google_cloud_tracing(env=None):
    """Configure once per worker process, before LiveKit creates the job trace.

    LiveKit attaches its own processors to this provider. Keep the provider alive
    across jobs; process exit shuts it down. Job callbacks flush only our exporter.
    """
    env = os.environ if env is None else env
    endpoint = env.get("GOOGLE_CLOUD_TRACE_ENDPOINT", "").strip()
    token = env.get("GOOGLE_CLOUD_TRACE_TOKEN", "").strip()
    if bool(endpoint) != bool(token):
        raise ValueError(
            "GOOGLE_CLOUD_TRACE_ENDPOINT and GOOGLE_CLOUD_TRACE_TOKEN are required together"
        )
    if not endpoint:
        return None
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/v1/traces"
    ):
        raise ValueError(
            "GOOGLE_CLOUD_TRACE_ENDPOINT must be an HTTPS /v1/traces URL "
            "without credentials or query parameters"
        )
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
        session=_CollectorSession(),
    )
    processor = BatchSpanProcessor(exporter, export_timeout_millis=17000)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "abita-s2s",
                "deployment.environment.name": env.get(
                    "LIVEKIT_AGENT_DEPLOYMENT", ""
                ).strip()
                or (
                    "production"
                    if env.get("NODE_ENV") == "production"
                    else "development"
                ),
            }
        )
    )
    provider.add_span_processor(processor)
    # Match abita_agent: retain conversation/tool content unless content capture
    # or the LiveKit project's mandatory redaction setting withholds it.
    set_tracer_provider(provider, allow_pii=True)
    logger.info("Google Cloud trace export enabled")
    return processor


def register_trace_flush(ctx) -> None:
    processor = ctx.proc.userdata.get("google_cloud_trace_processor")
    if processor is None:
        return

    async def flush():
        try:
            if not await asyncio.to_thread(processor.force_flush, 17000):
                logger.error("Google Cloud trace flush timed out")
        except Exception:
            logger.error("Google Cloud trace flush failed")

    ctx.add_shutdown_callback(flush)
