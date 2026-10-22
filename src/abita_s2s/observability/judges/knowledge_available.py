"""Missing office knowledge is a gap to fill, not proof of an agent mistake."""

from livekit.agents import ChatContext

QUESTION = {
    "type": "noul",
    "instructions": "Did the returned office knowledge contain the information needed to answer the caller's office question? Compare the caller's question with search_office_knowledge results, not the agent's answer. Answer FALSE if a completed search returned no_results, only said the requested information was not supplied, or returned information that did not answer the question. Judge only completed searches; a failed or missing search does not prove the knowledge was missing.",
    "criteria": {
        "true": "The completed office knowledge searches supplied the information needed to answer the questions they were meant to answer.",
        "false": "A completed office knowledge search did not supply the information needed to answer the caller's question.",
    },
}


def skip(history: ChatContext, closeout: dict) -> dict | None:
    """Skip only when the office knowledge tool was not called."""
    if not any(
        item.type in {"function_call", "function_call_output"}
        and item.name == "search_office_knowledge"
        for item in history.items
    ):
        return {"status": "not_applicable", "reason": "no_office_knowledge_search"}
    return None
