"""Synthetic backend responses, not a second implementation of participation rules."""


def decision(plan="Aetna", coverage="medical", office="spring_hill", **changes):
    return (
        dict(
            outcome="accepted",
            participation="accepted",
            planId=plan.casefold().replace(" ", "-"),
            canonicalPlan=plan,
            carrierCode="",
            coverageType=coverage,
            officeId=office,
            allowedProviders=["Dr. Example"],
            requirements=[],
            eligibility="not_checked",
            canSchedule=True,
            selfPay=plan == "Self Pay",
        )
        | changes
    )


def check_response(request):
    import json
    import httpx

    body = json.loads(request.content)
    plan = body["plan"]
    rejected = plan not in ("Aetna", "VSP", "Self Pay")
    return httpx.Response(
        200,
        json=decision(
            plan,
            body["coverageType"],
            **(
                dict(
                    outcome="needs_clarification",
                    participation="unknown",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                )
                if rejected
                else {}
            ),
        ),
    )
