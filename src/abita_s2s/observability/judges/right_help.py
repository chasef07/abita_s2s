"""Right help."""

QUESTION = {
    "type": "boolean",
    "instructions": "By the end of the call, did the caller get the right help for each request: either the request was completed (supported by a successful tool result), or the caller was given a correct, clearly explained next step, such as a saved staff request they were told about, or a transfer to staff when the agent could not handle the request? A transfer counts only if the agent could not reasonably handle the request itself. If the call ended before any request was resolved or handed off, answer false.",
    "criteria": {
        "true": "Every request was completed or handed off with a correct, clearly explained next step.",
        "false": "At least one request was left unresolved, handed off unnecessarily, or the next step was missing, wrong, or unclear.",
    },
}
