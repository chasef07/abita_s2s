"""Each piece of call state has one owner module. Every other module reads it or asks."""

import ast
import dataclasses
import unittest
from pathlib import Path

from abita_s2s.call_record import CallRecord
from abita_s2s.state import CallState

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"
FIELDS = {f.name for f in dataclasses.fields(CallState)}

OWNERS = {
    "call": "runtime",
    "reporter": "runtime",
    "plans": "runtime",
    "identity": "identity",
    "record": "state",
}
CALLERS = {
    "loaded": ("identity",),
    "revoke": ("scheduling",),
    "sent": ("call_record",),
    "settled": ("call_record", "scheduling"),
}
SETTERS = ("setattr", "delattr", "object.__setattr__")
MUTATORS = {
    "add",
    "append",
    "clear",
    "discard",
    "extend",
    "insert",
    "model_copy",
    "pop",
    "popitem",
    "remove",
    "reverse",
    "setdefault",
    "sort",
    "update",
}
RESOLVER = "ask PatientResolver in identity.py to change the patient"


def fix(field: str) -> str:
    if field == "identity":
        return RESOLVER
    return f"only {OWNERS[field]} writes it; move the change there"


def attributes(target: ast.expr):
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from attributes(element)
    elif isinstance(target, (ast.Starred, ast.Subscript)):
        yield from attributes(target.value)
    elif isinstance(target, ast.Attribute):
        yield target.attr


def violations(module: str, tree: ast.AST) -> list[tuple[int, str]]:
    found = []

    def changes(line: int, verb: str, field: str):
        if OWNERS.get(field, module) != module:
            found.append((line, f"{verb} CallState.{field}; {fix(field)}"))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.Delete)):
            verb = "assigns" if isinstance(node, ast.Assign) else "deletes"
            for target in node.targets:
                for field in attributes(target):
                    changes(node.lineno, verb, field)
        elif isinstance(node, ast.AugAssign):
            for field in attributes(node.target):
                changes(node.lineno, "increments", field)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            for field in attributes(node.target):
                changes(node.lineno, "assigns", field)
        elif isinstance(node, ast.Call):
            func, name = node.func, ast.unparse(node.func)
            named = node.args[1] if len(node.args) > 1 else None
            field = named.value if isinstance(named, ast.Constant) else None
            if isinstance(field, str) and name in SETTERS:
                changes(node.lineno, name, field)
            elif not isinstance(func, ast.Attribute):
                continue
            elif func.attr in CALLERS and module not in CALLERS[func.attr]:
                callers = " and ".join(CALLERS[func.attr])
                found.append(
                    (
                        node.lineno,
                        f"calls CallRecord.{func.attr}, which only {callers} may call",
                    )
                )
            elif func.attr in MUTATORS and isinstance(func.value, ast.Attribute):
                verb = (
                    "copies" if func.attr == "model_copy" else f"calls {func.attr} on"
                )
                changes(node.lineno, verb, func.value.attr)
    return sorted(found)


def ownership_violations() -> list[str]:
    return [
        f"{path.relative_to(PACKAGE)}:{line} {text}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in violations(
            path.relative_to(PACKAGE).parts[0].removesuffix(".py"),
            ast.parse(path.read_text()),
        )
    ]


def read_fields() -> set[str]:
    return {
        node.attr
        for path in PACKAGE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }


class StateOwnershipTests(unittest.TestCase):
    maxDiff = None

    def test_every_field_has_one_owner(self):
        self.assertEqual(set(OWNERS), FIELDS)

    def test_every_field_is_read_by_the_app(self):
        self.assertEqual(
            FIELDS - read_fields(), set(), "delete call state that only tests read"
        )

    def test_every_record_writer_names_its_callers(self):
        self.assertLessEqual(set(CALLERS), set(vars(CallRecord)))

    def test_call_state_changes_only_in_its_owner(self):
        self.assertEqual(ownership_violations(), [])

    def test_scan_flags_each_way_to_change_owned_state(self):
        source = """
def change(self, patient, context):
    self.state.identity = None
    patient.identity += 1
    del self.state.plans
    setattr(self.state, "reporter", None)
    self.state.record.model_copy(update={})
    context.userdata.record = None
    self.state.record.loaded(None, None)
    self.state.record.revoke("1")
    held, record = self.state.identity, self.state.record
"""
        self.assertEqual(
            [
                f"{line} {text}"
                for line, text in violations("scheduling", ast.parse(source))
            ],
            [
                f"3 assigns CallState.identity; {RESOLVER}",
                f"4 increments CallState.identity; {RESOLVER}",
                "5 deletes CallState.plans; only runtime writes it; move the change there",
                "6 setattr CallState.reporter; only runtime writes it; move the change there",
                "7 copies CallState.record; only state writes it; move the change there",
                "8 assigns CallState.record; only state writes it; move the change there",
                "9 calls CallRecord.loaded, which only identity may call",
            ],
        )
