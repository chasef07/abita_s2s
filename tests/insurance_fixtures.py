"""Synthetic backend responses, not a second implementation of participation rules."""

def decision(plan="Aetna", coverage="medical", office="spring_hill", **changes):
    return dict(outcome="accepted", participation="accepted", canonicalPlan=plan,
                carrierCode="", coverageType=coverage, officeId=office, routing="all_three",
                allowedProviders=["Dr. Example"], requirements=[], eligibility="not_checked",
                canSchedule=True, selfPay=plan == "Self Pay",
                answer="success: This office participates; active coverage is not verified.") | changes


def check_response(request):
    import json
    import httpx
    body = json.loads(request.content)
    plan = body["plan"]
    rejected = plan not in ("Aetna", "VSP", "Self Pay")
    return httpx.Response(200, json=decision(plan, body["coverageType"], **(
        dict(outcome="needs_clarification", participation="unknown", canonicalPlan="",
             canSchedule=False, answer="needs_input: Ask for the exact plan.")
        if rejected else {})))
