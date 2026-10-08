"""Claims backed."""

QUESTION = {
    "type": "noul",
    "instructions": "Was every statement about the caller's own records (a prescription, appointment, referral, or insurance on file), about staff availability, or about an action being done or completed (for example 'I'm updating that now' or 'I've sent that to the office') supported by a tool result available BEFORE the statement? Office hours, locations, providers, and policies are scored separately; ignore them here. A caller's words, the agent's own earlier statements, or an unrelated tool result are not support. A later lookup or correction does not erase an earlier unsupported statement.",
    "criteria": {
        "true": "Every statement about the caller's records, staff availability, or an action had a supporting tool result before it was made, or no such statements were made.",
        "false": "At least one statement about the caller's records, staff availability, or an action lacked a supporting tool result before it was made.",
    },
}
