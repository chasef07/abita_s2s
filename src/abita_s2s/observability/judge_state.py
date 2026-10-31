"""The state the jury and call labeler judge: the call plus only the policies their questions name."""

from datetime import datetime

from livekit.agents import ChatContext

from abita_s2s.offices import EASTERN
from abita_s2s.prompt import load_prompt

SPEAKERS = {"user": "caller", "assistant": "agent"}


def judge_state(
    history: ChatContext, office_knowledge: str, insurance_list: str
) -> dict:
    """Give judges the call as one line per turn or tool result, beside the call start and the policies the agent followed.

    A tool line sits where its result arrived, so judges see what the agent knew when.
    """
    started = datetime.fromtimestamp(history.items[0].created_at, EASTERN)
    calls = {
        item.call_id: item for item in history.items if item.type == "function_call"
    }
    answered = {
        item.call_id for item in history.items if item.type == "function_call_output"
    }
    lines = []
    for item in history.items:
        if item.type == "message" and item.role in SPEAKERS:
            if text := (item.text_content or "").strip():
                interrupted = " (interrupted)" if item.interrupted else ""
                lines.append(f"{SPEAKERS[item.role]}: {text}{interrupted}")
        elif item.type == "function_call" and item.call_id not in answered:
            lines.append(f"tool {item.name}({item.arguments}) -> no result")
        elif item.type == "function_call_output" and item.call_id in calls:
            call = calls[item.call_id]
            result = f"ERROR: {item.output}" if item.is_error else item.output
            lines.append(f"tool {call.name}({call.arguments}) -> {result}")
    return {
        "transcript": lines,
        "call_started": f"{started:%A, %Y-%m-%d %H:%M %Z} ({EASTERN.key})",
        "office_knowledge": office_knowledge,
        "insurance_list": insurance_list,
        "transfer_policy": load_prompt("transfer_policy"),
    }
