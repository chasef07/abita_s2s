"""Synthetic backend responses, not a second implementation of participation rules."""

from abita_s2s.insurance_list import OfficePlans


def plan_id(plan, coverage="medical"):
    if plan == "Self Pay":
        return f"self-pay-{coverage.replace('_', '-')}"
    return plan.casefold().replace(" ", "-")


def decision(plan="Aetna", coverage="medical", office="spring_hill", **changes):
    return (
        dict(
            outcome="accepted",
            participation="accepted",
            planId=plan_id(plan, coverage),
            canonicalPlan=plan,
            carrierCode="",
            coverageType=coverage,
            officeId=office,
            allowedProviders=["Dr. Example"],
            requirements=[],
            eligibility="not_checked",
            canSchedule=True,
            selfPay=plan == "Self Pay",
            reason="accepted",
        )
        | changes
    )


def listed(label, coverage="medical", **fields):
    return {
        "planId": plan_id(label, coverage),
        "label": label,
        "family": label.split()[0],
        "names": [],
        "carrierId": "car1",
        "allowedProviders": ["Dr. Example"],
        "outcome": "accepted",
        "requirements": [],
        "callerNotice": "",
        "acceptedAt": [],
        "selfPay": label == "Self Pay",
        **fields,
    }


def office_plans():
    """The list a call loads: the same plans `decision` describes, per visit type."""
    return OfficePlans.model_validate(
        {
            coverage: [listed(label, coverage) for label in labels]
            for coverage, labels in (
                ("medical", ("Aetna", "Test Insurance", "Self Pay")),
                ("routine_vision", ("VSP", "Test Insurance", "Self Pay")),
            )
        }
    )
