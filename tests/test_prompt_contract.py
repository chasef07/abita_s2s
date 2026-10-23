"""Prompts and tool descriptions name only registered tools and real sections, and state each rule once."""

import re
import unittest
from collections import defaultdict
from typing import get_args
from unittest.mock import Mock

from livekit.agents import llm
from livekit.agents.llm.utils import build_legacy_openai_schema

from abita_s2s.offices import SPRING_HILL
from abita_s2s.prompt import BACKEND_TOOLS, PROMPTS_DIR, load_prompt
from abita_s2s.prompt import speaker_instructions
from abita_s2s.tools.registry import TOOLS, Capability, Owners, offer

REGISTRY = "tools/registry.py TOOLS"
EVERY_OWNER = Owners(
    SPRING_HILL,
    Mock(),
    resolver=Mock(),
    scheduling=Mock(),
    insurance=Mock(),
    staff_tasks=Mock(),
    call_control=Mock(),
)
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
BACKTICKED = re.compile(r"`([A-Za-z_]\w*)[^`]*`")
SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
HEADING = re.compile(r"^#+ (.+?)\s*$", re.M)
SECTION_REFERENCE = re.compile(
    r"(?<=[a-z,;] )([A-Z][\w/-]*(?: [\w/-]+)*? (?:policy|rules|section))\b"
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
RULE_WORDS = 5


def schemas() -> dict[str, dict]:
    tools, _ = offer(EVERY_OWNER)
    return {
        name: build_legacy_openai_schema(tool, internally_tagged=True)
        for name, tool in llm.ToolContext(tools).function_tools.items()
        if isinstance(tool, llm.FunctionTool)
    }


def surfaces() -> dict[str, str]:
    """Every model-facing text, keyed by where an agent edits it."""
    texts = {
        f"prompts/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted(PROMPTS_DIR.glob("*.md"))
    }
    for name, schema in schemas().items():
        texts[f"{name} description"] = schema["description"]
        for arg, field in schema["parameters"]["properties"].items():
            if "description" in field:
                texts[f"{name}.{arg} description"] = field["description"]
    return texts


def values(node) -> set[str]:
    if isinstance(node, dict):
        found = {str(v) for v in node.get("enum", ())}
        if "const" in node:
            found.add(str(node["const"]))
        return found.union(*(values(child) for child in node.values()))
    if isinstance(node, list):
        return set().union(*(values(child) for child in node))
    return set()


def vocabulary() -> set[str]:
    """Tool names, argument names and argument values the model can see."""
    words = set()
    for name, schema in schemas().items():
        words.add(name)
        words.update(schema["parameters"]["properties"])
        words.update(values(schema["parameters"]))
    return words


def flat(text: str) -> str:
    return " ".join(PLACEHOLDER.sub(" ", text).split())


class PromptContractTests(unittest.TestCase):
    def test_every_registry_entry_builds_when_every_owner_is_present(self):
        tools, _ = offer(EVERY_OWNER)
        self.assertEqual(len(tools), len(TOOLS))

    def test_prompts_and_descriptions_name_only_registered_tools(self):
        known = vocabulary()
        found = []
        for surface, text in surfaces().items():
            text = PLACEHOLDER.sub(" ", text)
            named = set(BACKTICKED.findall(text)) | set(SNAKE_CASE.findall(text))
            found += [
                f"{surface} names `{word}`, which no tool in {REGISTRY} offers "
                "as a tool name, argument or value"
                for word in sorted(named - known)
            ]
        self.assertEqual(found, [])

    def test_section_references_name_an_existing_heading(self):
        texts = surfaces()
        headings = sorted(
            {
                heading
                for surface, text in texts.items()
                if surface.startswith("prompts/")
                for heading in HEADING.findall(text)
            }
        )
        found = []
        for surface, text in texts.items():
            for reference in SECTION_REFERENCE.findall(flat(text)):
                if not any(
                    reference == heading or reference.startswith(heading + " ")
                    for heading in headings
                ):
                    found.append(
                        f'{surface} refers to "{reference}", but no prompt has '
                        f"that heading; headings: {', '.join(headings)}"
                    )
        self.assertEqual(found, [])

    def test_each_rule_sentence_lives_in_one_surface(self):
        places = defaultdict(set)
        for surface, text in surfaces().items():
            for sentence in SENTENCE_END.split(flat(text)):
                if len(sentence.split()) >= RULE_WORDS:
                    places[sentence.lower()].add(surface)
        found = [
            f'"{sentence}" is repeated in {", ".join(sorted(where))}; keep it in one'
            for sentence, where in places.items()
            if len(where) > 1
        ]
        self.assertEqual(found, [])

    def test_speaker_lists_each_registry_capability_once(self):
        _, tail = load_prompt("speaker").split(BACKEND_TOOLS, 1)
        listed = [
            line.removeprefix("- ").split(":", 1)[0]
            for line in tail.split("\n\n", 1)[0].splitlines()
        ]
        self.assertEqual(sorted(listed), sorted(get_args(Capability)))
        backed = {capability for entry in TOOLS for capability in entry.capabilities}
        self.assertEqual(backed, set(get_args(Capability)))

    def test_speaker_lists_only_the_capabilities_a_call_offers(self):
        self.assertEqual(
            speaker_instructions(get_args(Capability)), load_prompt("speaker")
        )
        _, capabilities = offer(Owners(SPRING_HILL, Mock(), call_control=Mock()))
        speaker = speaker_instructions(capabilities)
        self.assertNotIn("- Appointments:", speaker)
        self.assertIn("- Call completion: end the call.", speaker)
