"""The seeded backend a replayed call reaches instead of Product and the middleware; it never opens a socket."""

import json
import re
from collections.abc import Callable

import httpx

from abita_s2s.config import Config

PRODUCT = "https://product.fixture.invalid"
MIDDLEWARE = "https://middleware.fixture.invalid"
KNOWLEDGE_URL = PRODUCT + "/v1/knowledge"
TASKS_URL = PRODUCT + "/v1/tasks"
CALLER_PHONE = "+13525550147"

TOPICS = {
    "Hours": re.compile(
        r"hour|open|clos|time|today|tomorrow|day|weekend|horario|abre|cierra", re.I
    ),
    "Location": re.compile(
        r"address|locat|where|direction|street|dirección|dónde", re.I
    ),
    "New patients": re.compile(
        r"bring|new patient|first visit|document|paperwork", re.I
    ),
}
OFFICE_FACTS = {
    "spring-hill": {
        "Hours": "Monday 9 AM to 4 PM. Tuesday through Friday 8 AM to 5 PM. "
        "Closed Saturday and Sunday.",
        "Location": "100 Fixture Way, Spring Hill, FL 34606.",
        "New patients": "Bring a photo ID, your insurance card, and your current "
        "glasses or contact lenses.",
    },
    "crystal-river": {
        "Hours": "Monday through Friday 8 AM to 5 PM. Closed Saturday and Sunday.",
        "Location": "200 Fixture Road, Crystal River, FL 34428.",
    },
}
PATIENT = {
    "status": "verified",
    "patientId": "fixture-1001",
    "name": "Lana Fixture",
    "dob": "04/16/1992",
    "appointmentsStatus": "found",
    "appointments": [
        {
            "id": 501,
            "date": "11/12/2026",
            "time": "10:30 AM",
            "provider": "Dr. Ana Fixture",
            "type": "Routine Eye Exam",
            "facility": "Spring Hill",
            "visitType": "routine_vision",
            "confirmed": True,
        }
    ],
}


def config(api_key: str) -> Config:
    """Agent settings with only the model key real. Service URLs point at this backend, and handoff and reporting stay unset."""
    return Config(
        openai_api_key=api_key,
        knowledge_url=KNOWLEDGE_URL,
        product_secret="fixture",
        middleware_url=MIDDLEWARE,
        middleware_token="fixture",
        staff_tasks_url=TASKS_URL,
    )


def _knowledge(request: httpx.Request, body: dict) -> dict:
    """The office's passages whose topic the query names, as Product's search returns only relevant ones."""
    facts = {
        title: text
        for title, text in OFFICE_FACTS.get(request.headers["X-Office-Key"], {}).items()
        if TOPICS[title].search(body["query"])
    }
    if not facts:
        return {"outcome": "no_relevant_information", "passages": []}
    return {
        "outcome": "found",
        "revisionId": "fixture-1",
        "passages": [
            {
                "revisionId": "fixture-1",
                "sectionId": f"section-{n}",
                "title": title,
                "text": text,
            }
            for n, (title, text) in enumerate(facts.items())
        ],
    }


def _resolve(_request: httpx.Request, body: dict) -> dict:
    dob = re.sub(r"\D", "", body.get("dob") or "")
    name = (body.get("firstName") or "").strip().lower()
    if name == "lana" and dob in ("04161992", "19920416"):
        return PATIENT
    return {"status": "not_found"}


def _decision(_request: httpx.Request, body: dict) -> dict:
    return {
        "outcome": "accepted",
        "participation": "accepted",
        "planId": "fixture-plan",
        "canonicalPlan": body["plan"],
        "carrierCode": "FIXTURE",
        "coverageType": body["coverageType"],
        "officeId": body["office"],
        "allowedProviders": ["Dr. Ana Fixture"],
        "requirements": [],
        "eligibility": "not_checked",
        "canSchedule": True,
        "selfPay": False,
        "reason": "accepted",
    }


def _task(_request: httpx.Request, body: dict) -> dict:
    return {
        "status": "created",
        "taskId": "00000000-0000-4000-8000-000000000001",
        "category": body["category"],
        "urgency": body["urgency"],
    }


ROUTES: dict[str, Callable[[httpx.Request, dict], dict]] = {
    KNOWLEDGE_URL: _knowledge,
    TASKS_URL: _task,
    MIDDLEWARE + "/api/patient/resolve": _resolve,
    MIDDLEWARE + "/api/insurance/decision": _decision,
}


class World:
    """Answers backend requests from ``ROUTES`` and records each request with no route."""

    def __init__(self) -> None:
        self.unrouted: list[str] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._answer))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        route = ROUTES.get(url)
        if route is None:
            self.unrouted.append(f"{request.method} {url}")
            return httpx.Response(501)
        return httpx.Response(200, json=route(request, json.loads(request.content)))
