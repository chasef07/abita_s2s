import asyncio
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import httpx
from livekit.agents.llm.tool_context import ToolContext

from abita_s2s.config import Config, load_config
from abita_s2s.integrations.product import Product
from abita_s2s.knowledge import UNAVAILABLE, read_office_knowledge
from abita_s2s.offices import SPRING_HILL, get_office_profile
from abita_s2s.prompt import thinker_instructions
from abita_s2s.state import CallContext, CallState
from abita_s2s.tools.registry import Owners, offer

CONFIG = Config(
    "offline",
    knowledge_url="https://product.example/v1/agent/knowledge/read",
    product_secret="test-secret",
)
STARTED = datetime(2026, 10, 8, 12, tzinfo=UTC)
SWEETWATER = get_office_profile("sweetwater")
KNOWLEDGE = {
    "spring-hill": {
        "revisionId": "00000000-0000-4000-8000-000000000001",
        "entries": [
            {
                "id": "hours",
                "title": "Hours",
                "text": "Status: available\nWeekdays 8:30 AM to 4:30 PM. Closed weekends.",
            },
            {
                "id": "records",
                "title": "Medical records requests",
                "text": "Fax records requests to 555-0142.",
            },
        ],
    },
    "sweetwater": {
        "revisionId": "00000000-0000-4000-8000-000000000002",
        "entries": [
            {
                "id": "hours",
                "title": "Sweetwater hours",
                "text": "Tuesday to Saturday 10 AM to 6 PM.",
            }
        ],
    },
}


def call_state(office=SPRING_HILL):
    return CallState(
        CallContext(
            call_id="test-call",
            session_started_at=datetime.now(UTC),
            customer_key="abita",
            called_office_key=office.key,
            caller_phone="+15555550101",
            called_number=office.trunk_numbers[0],
        )
    )


def by_office(requests):
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=KNOWLEDGE[request.headers["x-office-key"]])

    return handler


class OfficeKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def product(self, handler, config=CONFIG):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return Product(client, config)

    async def test_prompt_holds_every_entry_of_the_called_office_and_no_other(self):
        requests = []
        product = self.product(by_office(requests))
        for office, other in ((SPRING_HILL, SWEETWATER), (SWEETWATER, SPRING_HILL)):
            with self.subTest(office=office.key):
                text = await read_office_knowledge(product, office.key)
                instructions = thinker_instructions(STARTED, text)
                request = requests[-1]
                self.assertEqual(str(request.url), CONFIG.knowledge_url)
                self.assertEqual(request.headers["authorization"], "Bearer test-secret")
                self.assertEqual(request.headers["x-office-key"], office.key)
                self.assertEqual(json.loads(request.content), {})
                for entry in KNOWLEDGE[office.key]["entries"]:
                    self.assertIn(entry["title"], instructions)
                    self.assertIn(
                        entry["text"].removeprefix("Status: available\n"), instructions
                    )
                for entry in KNOWLEDGE[other.key]["entries"]:
                    self.assertNotIn(entry["text"], instructions)
                self.assertNotIn("Status:", instructions)
                self.assertNotIn(KNOWLEDGE[office.key]["revisionId"], instructions)
                self.assertNotIn(UNAVAILABLE, instructions)

    async def test_entries_keep_their_display_order(self):
        text = await read_office_knowledge(self.product(by_office([])), SPRING_HILL.key)
        self.assertEqual(
            text,
            "Hours\nWeekdays 8:30 AM to 4:30 PM. Closed weekends.\n\n"
            "Medical records requests\nFax records requests to 555-0142.",
        )

    async def test_failed_read_yields_the_unavailable_block_and_logs_its_cause(self):
        def offline(request):
            raise httpx.ConnectError("private transport detail")

        def respond(status, body):
            return lambda request: httpx.Response(status, json=body)

        valid = KNOWLEDGE["spring-hill"]
        cases = (
            (respond(503, {"error": "private-error-body"}), CONFIG, "http_503"),
            (respond(401, valid), CONFIG, "http_401"),
            (respond(302, valid), CONFIG, "http_302"),
            (respond(200, {"entries": valid["entries"]}), CONFIG, "invalid_response"),
            (
                respond(200, {**valid, "entries": [{"title": "Hours", "text": 4}]}),
                CONFIG,
                "invalid_response",
            ),
            (offline, CONFIG, "network"),
            (respond(200, valid), Config("offline"), "not_configured"),
        )
        for handler, config, cause in cases:
            with (
                self.subTest(cause=cause),
                self.assertLogs("abita_s2s.knowledge", level="ERROR") as logs,
            ):
                text = await read_office_knowledge(
                    self.product(handler, config), SPRING_HILL.key
                )
                self.assertEqual(text, UNAVAILABLE)
                self.assertIn(UNAVAILABLE, thinker_instructions(STARTED, text))
                self.assertIn(f"cause={cause}", logs.output[0])
                self.assertIn("office=spring-hill", logs.output[0])
                self.assertNotIn("private", str(logs.output))
                self.assertNotIn("test-secret", str(logs.output))

    async def test_timed_out_read_yields_the_unavailable_block_and_logs_timeout(self):
        cancelled = asyncio.Event()

        async def slow(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch("abita_s2s.integrations.product.KNOWLEDGE_READ_TIMEOUT", 0.01),
            self.assertLogs("abita_s2s.knowledge", level="ERROR") as logs,
        ):
            text = await read_office_knowledge(self.product(slow), SPRING_HILL.key)
        self.assertEqual(text, UNAVAILABLE)
        self.assertIn("cause=timeout", logs.output[0])
        self.assertTrue(cancelled.is_set())

    async def test_office_knowledge_search_is_not_offered_to_the_model(self):
        owners = Owners(
            SPRING_HILL,
            resolver=Mock(),
            scheduling=Mock(),
            insurance=Mock(),
            staff_tasks=Mock(),
            call_control=Mock(),
        )
        tools = ToolContext(offer(owners)).function_tools
        self.assertTrue(tools)
        self.assertNotIn("search_office_knowledge", tools)


class KnowledgeConfigTests(unittest.TestCase):
    BASE = {
        "OPENAI_API_KEY": "offline",
        "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "private-secret",
    }

    def load(self, url):
        with patch.dict(
            "os.environ",
            {**self.BASE, "ACUITY_PRODUCT_KNOWLEDGE_URL": url},
            clear=True,
        ):
            return load_config()

    def test_read_endpoint_is_derived_from_the_configured_search_endpoint(self):
        for url, expected in (
            (
                "https://product.example/v1/agent/knowledge/search",
                "https://product.example/v1/agent/knowledge/read",
            ),
            (
                "https://product.example/v1/agent/knowledge/search/",
                "https://product.example/v1/agent/knowledge/read",
            ),
            (
                "http://127.0.0.1:8000/v1/agent/knowledge/search",
                "http://127.0.0.1:8000/v1/agent/knowledge/read",
            ),
            (
                "http://[::1]:8000/v1/agent/knowledge/search",
                "http://[::1]:8000/v1/agent/knowledge/read",
            ),
        ):
            with self.subTest(url=url):
                config = self.load(url)
                self.assertEqual(config.knowledge_url, expected)
                self.assertNotIn("private-secret", repr(config))

    def test_configuration_requires_secret_and_secure_search_endpoint(self):
        for url in (
            "http://product.example/v1/agent/knowledge/search",
            "file:///tmp/v1/agent/knowledge/search",
            "https://user:password@product.example/v1/agent/knowledge/search",
            "https://product.example/v1/agent/knowledge/read",
            "https://product.example/knowledge",
            "https://product.example/v1/agent/knowledge/search?x=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(url)
        with patch.dict("os.environ", self.BASE, clear=True):
            self.assertIsNone(load_config().knowledge_url)
        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "offline",
                    "ACUITY_PRODUCT_KNOWLEDGE_URL": "https://product.example/v1/agent/knowledge/search",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            load_config()
