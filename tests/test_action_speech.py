"""An action tool hands off to its owner before it speaks or waits on speech."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from livekit.agents import llm
from livekit.agents.llm.utils import (
    function_arguments_to_pydantic_model,
    validated_arguments,
)
from test_patient_resolution import call_state

from abita_s2s.offices import SPRING_HILL
from abita_s2s.tools.registry import TOOLS, Owners


class Reached(Exception):
    """The tool reached an owner or a speech call; the message says which."""


class Owner:
    """Stops the tool at its first call into the owner."""

    def __init__(self, state, name: str):
        self.state = state
        self._name = name

    def __getattr__(self, method: str):
        def reached(*_, **__):
            raise Reached(f"owner {self._name}.{method}")

        return reached


def speech(name: str):
    def reached(*_, **__):
        raise Reached(f"speech {name}")

    return reached


def sample(node: dict, defs: dict):
    """One valid value for a JSON schema node, preferring a non-null one."""
    if "$ref" in node:
        return sample(defs[node["$ref"].rsplit("/", 1)[1]], defs)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    if options := node.get("anyOf"):
        return sample(
            next((o for o in options if o.get("type") != "null"), options[0]), defs
        )
    return {"string": "x", "boolean": True, "integer": 1, "number": 1}[node["type"]]


def arguments(tool: llm.FunctionTool) -> dict:
    schema = function_arguments_to_pydantic_model(tool).model_json_schema()
    defs = schema.get("$defs", {})
    return validated_arguments(
        tool,
        {
            name: sample(node, defs)
            for name, node in schema.get("properties", {}).items()
        },
    )


def action_tools() -> dict[str, llm.FunctionTool]:
    state = call_state()
    owners = Owners(
        SPRING_HILL,
        resolver=Owner(state, "resolver"),
        scheduling=Owner(state, "scheduling"),
        insurance=Owner(state, "insurance"),
        staff_tasks=Owner(state, "staff_tasks"),
        call_control=Owner(state, "call_control"),
    )
    tools = [
        tool
        for entry in TOOLS
        if entry.action and (tool := entry.build(owners)) is not None
    ]
    return {
        name: tool
        for name, tool in llm.ToolContext(tools).function_tools.items()
        if isinstance(tool, llm.FunctionTool)
    }, state


class ActionSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_tools_reach_their_owner_before_any_speech(self):
        tools, state = action_tools()
        self.assertEqual(
            set(tools),
            {
                "book_appointment",
                "cancel_appointment",
                "reschedule_appointment",
                "save_staff_task",
                "transfer_call",
                "end_call",
                "add_patient",
                "update_insurance",
            },
            "every tool that writes or moves the call sets action=True "
            "on its Entry in tools/registry.py",
        )
        context = SimpleNamespace(
            userdata=state,
            function_call=SimpleNamespace(call_id="call"),
            disallow_interruptions=Mock(),
            wait_for_playout=speech("wait_for_playout"),
            session=SimpleNamespace(
                say=speech("say"), generate_reply=speech("generate_reply")
            ),
        )
        first = {}
        for name, tool in tools.items():
            try:
                await tool(context, **arguments(tool))
                first[name] = "nothing"
            except Reached as reached:
                first[name] = str(reached)
        self.assertEqual(
            {name: step for name, step in first.items() if step.startswith("speech")},
            {},
            "an action tool speaks or waits on speech before it calls its owner; "
            "call the owner first and let it bound any announcement in budgets.py",
        )
        self.assertEqual(
            {name for name, step in first.items() if step == "nothing"}, set()
        )


if __name__ == "__main__":
    unittest.main()
