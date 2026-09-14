import asyncio
import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from livekit.agents.llm.utils import build_strict_openai_schema

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config, load_config
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OFFICES, SPRING_HILL
from abita_s2s.state import CallContext, CallState

CONFIG = Config(
    "offline",
    knowledge_url="https://product.example/v1/agent/knowledge/search",
    product_secret="test-secret",
)
FOUND = {
    "outcome": "found",
    "revisionId": "revision-1",
    "passages": [
        {
            "revisionId": "revision-1",
            "sectionId": "hours",
            "title": "Hours",
            "text": "Status: available\nWeekdays 8:30 AM–4:30 PM. Closed weekends.",
        }
    ],
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


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def knowledge(self, handler, config=CONFIG):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return OfficeKnowledge(client, config)

    async def test_registered_tool_uses_call_office_and_sends_only_question(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=FOUND)

        knowledge = self.knowledge(handler)
        for office in OFFICES:
            with self.subTest(office=office.key):
                # Deliberately use a different greeting profile: routing belongs to call state.
                agent = AbitaAgent(SPRING_HILL, knowledge)
                schema = build_strict_openai_schema(agent.tools[0])["function"]
                self.assertEqual(schema["name"], "search_office_knowledge")
                self.assertEqual(set(schema["parameters"]["properties"]), {"query"})
                self.assertFalse(schema["parameters"]["additionalProperties"])
                answer = json.loads(
                    await agent.search_office_knowledge(
                        SimpleNamespace(userdata=call_state(office)),
                        "  When do you close?  ",
                    )
                )
                request = requests[-1]
                self.assertEqual(str(request.url), CONFIG.knowledge_url)
                self.assertEqual(request.headers["authorization"], "Bearer test-secret")
                self.assertEqual(request.headers["x-office-key"], office.key)
                self.assertEqual(
                    json.loads(request.content), {"query": "When do you close?"}
                )
                self.assertEqual(
                    answer,
                    {
                        "office": office.key,
                        "query": "When do you close?",
                        "outcome": "found",
                        "answer": "Weekdays 8:30 AM–4:30 PM. Closed weekends.",
                    },
                )

    async def test_no_information_is_distinct_from_unavailability(self):
        for outcome in ("no_relevant_information", "temporary_failure"):
            with self.subTest(outcome=outcome):
                knowledge = self.knowledge(
                    lambda request, outcome=outcome: httpx.Response(
                        200, json={"outcome": outcome, "passages": []}
                    )
                )
                result = await knowledge.search(
                    "spring-hill", "Do you offer this service?"
                )
                self.assertEqual(result["outcome"], outcome)

    async def test_invalid_query_lengths_never_reach_backend(self):
        def unexpected(request):
            self.fail("Invalid query reached Product")

        knowledge = self.knowledge(unexpected)
        for query in (
            "",
            "  a ",
            "x" * 501,
        ):
            with self.subTest(query=query):
                result = await knowledge.search("spring-hill", query)
                self.assertEqual(result["outcome"], "invalid_query")
                self.assertNotIn("query", result)

    async def test_malformed_or_inconsistent_evidence_is_unavailable(self):
        mixed = deepcopy(FOUND)
        mixed["passages"][0]["revisionId"] = "other-revision"
        empty = deepcopy(FOUND)
        empty["passages"][0]["text"] = "Status: available"
        cases = [
            mixed,
            empty,
            {},
            {**FOUND, "revisionId": None},
            {**FOUND, "passages": []},
            {**FOUND, "passages": FOUND["passages"] * 9},
            {**FOUND, "outcome": "no_relevant_information"},
            {**FOUND, "passages": [{**FOUND["passages"][0], "text": 42}]},
        ]
        for body in cases:
            with self.subTest(body=body):
                knowledge = self.knowledge(
                    lambda request, body=body: httpx.Response(200, json=body)
                )
                result = await knowledge.search("spring-hill", "Hours?")
                self.assertEqual(result["outcome"], "temporary_failure")
                self.assertNotIn("4:30", result["answer"])

    async def test_http_errors_redirects_and_bad_json_never_become_answers(self):
        for status in (302, 401, 403, 429, 503, 200):
            requests = []

            def handler(request, requests=requests, status=status):
                requests.append(request)
                return httpx.Response(
                    status,
                    headers={"location": "https://other.example"},
                    text="private-error-body",
                )

            with (
                self.subTest(status=status),
                self.assertLogs("abita_s2s.knowledge", level="WARNING") as logs,
            ):
                result = await self.knowledge(handler).search("spring-hill", "Hours?")
                self.assertEqual(result["outcome"], "temporary_failure")
                self.assertEqual(len(requests), 1)
                self.assertNotIn("private-error-body", str(logs.output))
                self.assertNotIn("test-secret", str(logs.output))

    async def test_network_failure_and_missing_configuration_are_unavailable(self):
        def offline(request):
            raise httpx.ConnectError("private transport detail")

        self.assertEqual(
            (await self.knowledge(offline).search("spring-hill", "Hours?"))["outcome"],
            "temporary_failure",
        )

        def unexpected(request):
            self.fail("Unconfigured knowledge sent a request")

        result = await self.knowledge(unexpected, Config("offline")).search(
            "spring-hill", "Hours?"
        )
        self.assertEqual(result["outcome"], "temporary_failure")

    async def test_deadline_cancels_read_and_external_cancellation_propagates(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        knowledge = self.knowledge(slow)
        with patch("abita_s2s.knowledge.SEARCH_TIMEOUT", 0.01):
            result = await knowledge.search("spring-hill", "Hours?")
        self.assertEqual(result["outcome"], "temporary_failure")
        self.assertTrue(cancelled.is_set())
        started.clear()
        cancelled.clear()
        task = asyncio.create_task(knowledge.search("spring-hill", "Hours?"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_overlapping_results_retain_their_original_question(self):
        first_started, release_first = asyncio.Event(), asyncio.Event()

        async def handler(request):
            query = json.loads(request.content)["query"]
            if query == "Weekday hours?":
                first_started.set()
                await release_first.wait()
            body = deepcopy(FOUND)
            body["passages"][0]["text"] = (
                "Closed weekends."
                if query == "Actually, Saturday hours?"
                else "Weekdays close at 4:30 PM."
            )
            return httpx.Response(200, json=body)

        knowledge = self.knowledge(handler)
        first = asyncio.create_task(knowledge.search("spring-hill", "Weekday hours?"))
        await asyncio.wait_for(first_started.wait(), 1)
        latest = await knowledge.search("spring-hill", "Actually, Saturday hours?")
        release_first.set()
        earlier = await first
        self.assertEqual(latest["query"], "Actually, Saturday hours?")
        self.assertEqual(latest["answer"], "Closed weekends.")
        self.assertEqual(earlier["query"], "Weekday hours?")
        self.assertEqual(earlier["answer"], "Weekdays close at 4:30 PM.")


class KnowledgeConfigTests(unittest.TestCase):
    def test_configuration_requires_pair_and_secure_endpoint(self):
        base = {
            "OPENAI_API_KEY": "offline",
            "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "private-secret",
        }
        for url in (
            "",
            "http://product.example/search",
            "file:///tmp/knowledge",
            "https://user:password@product.example/search",
        ):
            with (
                self.subTest(url=url),
                patch.dict(
                    "os.environ",
                    {**base, "ACUITY_PRODUCT_KNOWLEDGE_URL": url},
                    clear=True,
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
        for url in (
            CONFIG.knowledge_url,
            "http://127.0.0.1:8000/search",
            "http://[::1]:8000/search",
        ):
            with (
                self.subTest(url=url),
                patch.dict(
                    "os.environ",
                    {**base, "ACUITY_PRODUCT_KNOWLEDGE_URL": url},
                    clear=True,
                ),
            ):
                config = load_config()
                self.assertEqual(config.knowledge_url, url)
                self.assertNotIn("private-secret", repr(config))
