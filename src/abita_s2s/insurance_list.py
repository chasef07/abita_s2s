"""Read the called office's insurance plans once per call, as text for the thinker's prompt."""

import asyncio
import logging
import re
from collections.abc import Sequence

from abita_s2s.integrations.registration_middleware import (
    PlanSummary,
    RegistrationMiddleware,
)
from abita_s2s.records import Record

logger = logging.getLogger(__name__)

UNAVAILABLE = "The insurance list could not be loaded for this call."

REQUIREMENTS = {
    "pcp_referral": "a referral from the primary care doctor",
    "prior_authorization": "prior authorization",
    "staff_verify": "coverage",
}
WORD = re.compile(r"[a-z0-9]+")
FILLER = frozenset(
    {"medical", "vision", "the", "of", "and", "plan", "health", "insurance"}
)


class OfficePlans(Record):
    """Every plan the office lists for each coverage, in middleware order."""

    medical: list[PlanSummary]
    routine_vision: list[PlanSummary]


def render_insurance_list(plans: OfficePlans) -> str:
    """Medical then routine vision plans, grouped by company under level 3 and 4 headings.

    Headings are level 3 because the list sits under the prompt's `## Insurance list`.
    """
    return "\n\n".join(
        (
            _section("Medical visits", plans.medical),
            _section("Routine vision exams", plans.routine_vision),
        )
    )


async def read_insurance_list(
    middleware: RegistrationMiddleware, office_key: str
) -> str:
    """Both coverages' plans as prompt text, or the unavailable notice with each cause logged."""
    medical, routine_vision = await asyncio.gather(
        middleware.plans(office_key, "medical"),
        middleware.plans(office_key, "routine_vision"),
    )
    if isinstance(medical, str) or isinstance(routine_vision, str):
        causes = (("medical", medical), ("routine_vision", routine_vision))
        logger.error(
            "Insurance list unavailable office=%s cause=%s",
            office_key,
            ",".join(
                f"{coverage}:{cause}"
                for coverage, cause in causes
                if isinstance(cause, str)
            ),
        )
        return UNAVAILABLE
    logger.info(
        "Insurance list loaded office=%s medical=%d routine_vision=%d",
        office_key,
        len(medical.plans),
        len(routine_vision.plans),
    )
    return render_insurance_list(
        OfficePlans(medical=medical.plans, routine_vision=routine_vision.plans)
    )


def _section(title: str, plans: Sequence[PlanSummary]) -> str:
    if not plans:
        return (
            f"### {title}\n\nThis office does not accept insurance for {title.lower()}."
        )
    families: dict[str, list[PlanSummary]] = {}
    for plan in plans:
        families.setdefault(plan.family, []).append(plan)
    return "\n\n".join(
        (
            f"### {title}",
            *(
                f"#### {family}{_verdict(families[family])}\n\n"
                + "\n".join(_line(plan) for plan in families[family])
                for family in sorted(families, key=str.casefold)
            ),
        )
    )


def _verdict(plans: Sequence[PlanSummary]) -> str:
    """A company-wide answer; different carrier IDs are different payers on the chart."""
    if len(plans) == 1:
        return ""
    if all(plan.outcome == "accepted" and not plan.requirements for plan in plans):
        if len({plan.carrierId for plan in plans}) == 1:
            return ": all accepted"
        return ": all accepted; ask which plan is on the card before registering"
    if all(plan.outcome == "not_accepted" for plan in plans):
        return ": none accepted"
    return ": varies by plan; ask which plan is on the card"


def _line(plan: PlanSummary) -> str:
    line = f"- {plan.label} `{plan.planId}`: {_status(plan)}"
    aliases = _aliases(plan)
    if aliases:
        line += f" (also said as: {'; '.join(aliases)})"
    return line


def _status(plan: PlanSummary) -> str:
    """Middleware names doctors only for a plan it accepts, so staff checks without them are unconfirmed."""
    if plan.outcome == "accepted":
        status = "accepted"
    elif plan.outcome == "not_accepted":
        status = "not accepted"
        if plan.acceptedAt:
            status += f" here (accepted at {', '.join(plan.acceptedAt)})"
    elif plan.requirements and plan.allowedProviders:
        checks = " and ".join(REQUIREMENTS[kind] for kind in plan.requirements)
        status = f"accepted; office staff must verify {checks} before scheduling"
    else:
        status = "not confirmed yet; office staff confirm before scheduling"
    if plan.callerNotice:
        status += f". Note: {plan.callerNotice}"
    return status


def _aliases(plan: PlanSummary) -> list[str]:
    """Names that add words beyond the label and the names already kept."""
    kept, known = [], [_words(plan.label)]
    for name in plan.names:
        words = _words(name)
        if words and not any(words <= other for other in known):
            kept.append(name)
            known.append(words)
    return kept


def _words(text: str) -> set[str]:
    return set(WORD.findall(text.casefold().replace("&", " and "))) - FILLER
