"""Deterministic participation matching, ported from abita_agent insurance-rules.ts."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from abita_s2s.insurance_state import CoverageType

SOURCES = {
    "spring-hill": ("SPRING_HILL_MEDICAL", "SPRING_HILL_ROUTINE_VISION"),
    "crystal-river": ("CRYSTAL_RIVER", None),
    "hollywood": ("HOLLYWOOD_SWEETWATER", "SPRING_HILL_ROUTINE_VISION"),
    "sweetwater": ("HOLLYWOOD_SWEETWATER", "SPRING_HILL_ROUTINE_VISION"),
    "north-miami-beach-optical": (None, "SPRING_HILL_ROUTINE_VISION"),
}


class _Candidate(NamedTuple):
    rule: dict
    term: str
    normalized_term: str
    source: str
    exact: bool


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip()


@lru_cache
def reference(source: str) -> dict:
    return json.loads(
        (
            Path(__file__).parent / "insurance_data" / f"INSURANCE_{source}.json"
        ).read_text()
    )


def contains(query: str, term: str) -> bool:
    return f" {term} " in f" {query} "


def match_plan(office: str, query: str, coverage: CoverageType) -> dict:
    if coverage not in ("medical", "routine_vision"):
        raise ValueError("Unsupported visit type")
    medical_source, vision_source = SOURCES[office]
    source = vision_source if coverage == "routine_vision" else medical_source
    if source is None:
        return {
            "outcome": "not_accepted",
            "answer": f"No, we don't accept {query} for this visit type at this office.",
            "canonical_plan": None,
        }
    normalized = normalize(query)
    candidates = []
    for rule in reference(source)["plans"]:
        display = rule.get("displayName") or rule.get("canonicalPlan")
        for term, kind in (
            [(display, "display")]
            + [(a, "alias") for a in rule.get("aliases", [])]
            + [(a, "required") for a in rule.get("requiredWordAliases", [])]
        ):
            term_normalized = normalize(term or "")
            if not term_normalized:
                continue
            matches = (
                set(term_normalized.split()) <= set(normalized.split())
                if kind == "required"
                else contains(normalized, term_normalized)
            )
            if matches:
                candidates.append(
                    _Candidate(
                        rule,
                        query if kind == "required" else term,
                        term_normalized,
                        kind,
                        normalized == term_normalized,
                    )
                )

    def rank(c):
        return c.exact, len(c.normalized_term), 2 if c.source == "display" else 1

    def best(items):
        return max(items, key=rank, default=None)

    selected = best([c for c in candidates if c.source == "required"]) or best(
        [c for c in candidates if c.exact]
    )
    if selected is None:
        rejected = best([c for c in candidates if c.rule["status"] == "not_accepted"])
        accepted = best([c for c in candidates if c.rule["status"] == "accepted"])
        followup = best(
            [
                c
                for c in candidates
                if c.rule["status"] in ("needs_clarification", "needs_staff_task")
            ]
        )
        if rejected and not (
            accepted
            and rank(accepted) > rank(rejected)
            and contains(accepted.normalized_term, rejected.normalized_term)
        ):
            selected = rejected
        elif accepted and (not followup or rank(accepted) > rank(followup)):
            selected = accepted
        else:
            selected = followup or accepted
    if selected is None:
        return clarification("the exact plan name from the insurance card")
    rule, term, kind = selected.rule, selected.term, selected.source
    if rule["status"] == "needs_clarification":
        return clarification(
            rule.get("clarificationNeeded")
            or "the exact plan name from the insurance card"
        )
    canonical = rule.get("canonicalPlan") or rule.get("displayName") or term
    display = rule.get("displayName") or rule.get("canonicalPlan")
    caller_plan = (
        term
        if kind != "display"
        and (not display or normalize(display) == normalize(canonical))
        else display or canonical or term
    )
    notice = rule.get("callerNotice") or ""
    status = "needs_staff_task" if rule.get("preauthRequired") else rule["status"]
    if status == "accepted":
        answer = f"Yes, we take {caller_plan}." + (f" {notice}" if notice else "")
    elif status == "not_accepted":
        answer = f"No, we don't accept {caller_plan}."
    elif rule.get("preauthRequired"):
        answer = (
            (f"{notice} " if notice else "")
            + "This plan requires prior authorization before scheduling. Ask permission for staff follow-up."
        )
    else:
        answer = (notice or "The office needs to confirm this coverage").rstrip(
            ".!?"
        ) + ". Ask permission for staff follow-up before scheduling."
    return {
        "outcome": status,
        "answer": answer,
        "canonical_plan": canonical
        if status == "accepted" and rule["canProceed"]
        else None,
    }


def clarification(detail: str) -> dict:
    return {
        "outcome": "needs_clarification",
        "answer": f"I can check that, but I need to know {detail.rstrip('.!?')}.",
        "canonical_plan": None,
    }
