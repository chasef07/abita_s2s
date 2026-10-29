"""Each piece of call state has one owner module. Every other module reads it or asks."""

import ast
import dataclasses
import inspect
import unittest
from pathlib import Path

from abita_s2s.insurance_state import InsuranceState
from abita_s2s.state import CallState, PatientState

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"
STATE_CLASSES = (CallState, PatientState, InsuranceState)
FIELD_CLASS = {
    f.name: cls.__name__ for cls in STATE_CLASSES for f in dataclasses.fields(cls)
}

OWNERS = {
    "call": "runtime",
    "reporter": "runtime",
    "patient": "state",
    "insurance": "state",
    "lookup": "state",
    "active": "state",
    "revision": "state",
    "absence": "state",
    "plans": "runtime",
    "registrations": "insurance",
    "write_pending": "insurance",
    "write_uncertain": "insurance",
}
CALLERS = {
    "activate": ("identity",),
    "deactivate": ("identity",),
    "update_insurance": ("identity",),
    "record_absence": ("identity",),
    "record_lookup": ("identity",),
    "update_appointments": ("scheduling",),
    "mark_appointments_stale": ("scheduling",),
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


def fix(field: str, module: str) -> str:
    if FIELD_CLASS[field] == "PatientState":
        methods = [m for m, callers in CALLERS.items() if module in callers]
        return f"call PatientState.{' or '.join(methods)}" if methods else RESOLVER
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
            found.append(
                (line, f"{verb} {FIELD_CLASS[field]}.{field}; {fix(field, module)}")
            )

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
                        f"calls PatientState.{func.attr}, which only {callers}"
                        f" may call; {RESOLVER}",
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


class StateOwnershipTests(unittest.TestCase):
    maxDiff = None

    def test_every_field_has_one_owner(self):
        names = [f.name for cls in STATE_CLASSES for f in dataclasses.fields(cls)]
        self.assertEqual(len(names), len(set(names)), "field names must be unique")
        self.assertEqual(set(OWNERS), set(names))

    def test_every_patient_method_names_its_callers(self):
        methods = {
            name
            for name, member in vars(PatientState).items()
            if inspect.isfunction(member) and not name.startswith("_")
        }
        self.assertEqual(set(CALLERS), methods)

    def test_call_state_changes_only_in_its_owner(self):
        self.assertEqual(ownership_violations(), [])

    def test_scan_flags_each_way_to_change_owned_state(self):
        source = """
def change(self, patient, context):
    self.state.patient.active = None
    patient.revision += 1
    self.state.insurance.registrations["1"] = "created"
    self.state.insurance.registrations.pop("1")
    del self.state.insurance.plans
    setattr(self.state.insurance, "write_pending", True)
    self.state.patient.active.model_copy(update={})
    context.userdata.patient = None
    self.state.patient.activate(None)
    self.state.patient.mark_appointments_stale()
    held, ins = self.state.patient, self.state.insurance
"""
        self.assertEqual(
            [
                f"{line} {text}"
                for line, text in violations("scheduling", ast.parse(source))
            ],
            [
                "3 assigns PatientState.active;"
                " call PatientState.update_appointments or mark_appointments_stale",
                "4 increments PatientState.revision;"
                " call PatientState.update_appointments or mark_appointments_stale",
                "5 assigns InsuranceState.registrations;"
                " only insurance writes it; move the change there",
                "6 calls pop on InsuranceState.registrations;"
                " only insurance writes it; move the change there",
                "7 deletes InsuranceState.plans;"
                " only runtime writes it; move the change there",
                "8 setattr InsuranceState.write_pending;"
                " only insurance writes it; move the change there",
                "9 copies PatientState.active;"
                " call PatientState.update_appointments or mark_appointments_stale",
                "10 assigns CallState.patient; only state writes it; move the change there",
                "11 calls PatientState.activate, which only identity may call;"
                f" {RESOLVER}",
            ],
        )
