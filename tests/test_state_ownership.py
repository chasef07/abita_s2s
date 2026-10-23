"""Each piece of call state has one owner module. Every other module reads it or asks."""

import ast
import dataclasses
import inspect
import unittest
from pathlib import Path

from abita_s2s.insurance_state import InsuranceState
from abita_s2s.state import CallState, PatientState

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"

OWNERS = {
    "CallState": {
        "call": "runtime",
        "reporter": "runtime",
        "patient": "state",
        "insurance": "state",
    },
    "PatientState": {
        "lookup": "state",
        "active": "state",
        "revision": "state",
        "absence": "state",
    },
    "InsuranceState": {
        "accepted": "insurance_state",
        "check_revision": "insurance_state",
        "offered": "insurance",
        "registrations": "insurance",
        "write_pending": "insurance",
        "write_uncertain": "insurance",
    },
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
TRANSITION_MODULES = ("state", "insurance_state")
PRIVATE_READS = {("InsuranceState", "accepted"): "accepted_insurance(state)"}
SLOTS = {
    ("CallState", "patient"): "PatientState",
    ("CallState", "insurance"): "InsuranceState",
}
UNTRACKED_INSIDE = {("CallState", "call"), ("CallState", "reporter")}
STATE_NAMES = ("state", "userdata")
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
    "__delitem__",
    "__setattr__",
    "__setitem__",
}

Reached = str | tuple[str, str] | None


def annotated_class(annotation: ast.expr | None) -> str | None:
    name = ast.unparse(annotation).strip("'\"") if annotation else None
    return name if name in OWNERS else None


def flatten(node: ast.expr):
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from flatten(element)
    elif isinstance(node, ast.Starred):
        yield from flatten(node.value)
    else:
        yield node


class Scan(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.names: list[dict[str, Reached]] = [{}]
        self.functions = ["<module>"]
        self.writes: list[tuple[str, str, int, str, str]] = []
        self.calls: list[tuple[str, int]] = []
        self.reads: list[tuple[str, str, int]] = []

    def reach(self, node: ast.expr) -> Reached:
        """The state class an expression is, or the (class, field) it reads from."""
        if isinstance(node, ast.Name):
            default = "CallState" if node.id in STATE_NAMES else None
            return self.names[-1].get(node.id, default)
        if isinstance(node, ast.Subscript):
            base = self.reach(node.value)
            return base if isinstance(base, tuple) else None
        if isinstance(node, ast.Attribute):
            base = self.reach(node.value)
            if isinstance(base, tuple):
                return base
            if (base, node.attr) in UNTRACKED_INSIDE:
                return None
            if (base, node.attr) in SLOTS:
                return SLOTS[(base, node.attr)]
            if isinstance(base, str) and node.attr in OWNERS[base]:
                return (base, node.attr)
            if base is None and node.attr in STATE_NAMES:
                return "CallState"
        return None

    def bind(self, names: dict[str, Reached], target: ast.expr, value: ast.expr):
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
            for t, v in zip(target.elts, value.elts):
                self.bind(names, t, v)
        elif isinstance(target, ast.Name) and (reached := self.reach(value)):
            names[target.id] = reached

    def visit_ClassDef(self, node: ast.ClassDef):
        cls = node.name if node.name in OWNERS else None
        self.names.append({**self.names[-1], "self": cls})
        self.generic_visit(node)
        self.names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        names = dict(self.names[-1])
        arguments = node.args
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            if cls := annotated_class(arg.annotation):
                names[arg.arg] = cls
        self.names.append(names)
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    self.bind(names, target, child.value)
            elif isinstance(child, ast.NamedExpr):
                self.bind(names, child.target, child.value)
            elif isinstance(child, ast.AnnAssign) and isinstance(
                child.target, ast.Name
            ):
                if cls := annotated_class(child.annotation):
                    names[child.target.id] = cls
                elif child.value is not None:
                    self.bind(names, child.target, child.value)
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()
        self.names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def write(self, reached: Reached, field: str | None, line: int, verb: str):
        if isinstance(reached, tuple):
            self.writes.append((*reached, line, verb, self.functions[-1]))
        elif isinstance(reached, str) and field in OWNERS[reached]:
            self.writes.append((reached, field, line, verb, self.functions[-1]))

    def assigned(self, target: ast.expr, verb: str, line: int):
        for node in flatten(target):
            if isinstance(node, ast.Attribute):
                self.write(self.reach(node.value), node.attr, line, verb)
            elif isinstance(node, ast.Subscript):
                reached = self.reach(node.value)
                self.write(
                    reached if isinstance(reached, tuple) else None, None, line, verb
                )

    def visit_Assign(self, node: ast.Assign):
        for target in node.targets:
            self.assigned(target, "assigns", node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        self.assigned(node.target, "increments", node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None:
            self.assigned(node.target, "assigns", node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete):
        for target in node.targets:
            self.assigned(target, "deletes", node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            base = self.reach(func.value)
            if isinstance(base, tuple) and func.attr in MUTATORS:
                verb = (
                    "copies" if func.attr == "model_copy" else f"calls {func.attr} on"
                )
                self.write(base, None, node.lineno, verb)
            elif base == "PatientState" and func.attr in CALLERS:
                self.calls.append((func.attr, node.lineno))
        name = ast.unparse(func)
        if name in ("setattr", "delattr", "object.__setattr__") and node.args:
            field = node.args[1] if len(node.args) > 1 else None
            attribute = field.value if isinstance(field, ast.Constant) else None
            self.write(self.reach(node.args[0]), str(attribute), node.lineno, name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        base = self.reach(node.value)
        if isinstance(node.ctx, ast.Load) and isinstance(base, str):
            if (base, node.attr) in PRIVATE_READS:
                self.reads.append((base, node.attr, node.lineno))
        self.generic_visit(node)


def scan(path: Path) -> Scan:
    found = Scan(path.relative_to(PACKAGE).parts[0].removesuffix(".py"))
    found.visit(ast.parse(path.read_text()))
    return found


def violations(path: str, found: Scan, writers: dict[tuple[str, str], set[str]]):
    lines = []
    for cls, field, line, verb, _ in found.writes:
        owner = OWNERS[cls][field]
        if found.module == owner:
            continue
        methods = sorted(writers.get((cls, field), ()))
        if cls == "PatientState":
            methods = [m for m in methods if found.module in CALLERS.get(m, ())]
            fix = (
                f"call PatientState.{' or '.join(methods)}"
                if methods
                else "add a PatientState method that makes this change"
                if found.module == "identity"
                else "ask PatientResolver in identity.py to change the patient"
            )
        elif methods and owner in TRANSITION_MODULES:
            fix = f"call {' or '.join(methods)} from {owner}.py"
        else:
            fix = f"only {owner} writes it; move the change there"
        lines.append((line, f"{path}:{line} {verb} {cls}.{field}; {fix}"))
    for method, line in found.calls:
        if found.module not in ("state", *CALLERS[method]):
            lines.append(
                (
                    line,
                    f"{path}:{line} calls PatientState.{method}, which only"
                    f" {' and '.join(CALLERS[method])} may call;"
                    " ask PatientResolver in identity.py to change the patient",
                )
            )
    for cls, field, line in found.reads:
        if found.module != OWNERS[cls][field]:
            fix = PRIVATE_READS[(cls, field)]
            lines.append((line, f"{path}:{line} reads {cls}.{field}; call {fix}"))
    return [text for _, text in sorted(lines)]


def ownership_violations() -> list[str]:
    scans = {
        str(p.relative_to(PACKAGE)): scan(p) for p in sorted(PACKAGE.rglob("*.py"))
    }
    writers: dict[tuple[str, str], set[str]] = {}
    for found in scans.values():
        for cls, field, _, _, function in found.writes:
            if found.module == OWNERS[cls][field] and not function.startswith("_"):
                writers.setdefault((cls, field), set()).add(function)
    return [
        line
        for path, found in scans.items()
        for line in violations(path, found, writers)
    ]


class StateOwnershipTests(unittest.TestCase):
    maxDiff = None

    def test_every_field_has_one_owner(self):
        for cls in (CallState, PatientState, InsuranceState):
            fields = {f.name for f in dataclasses.fields(cls)}
            self.assertEqual(set(OWNERS[cls.__name__]), fields, cls.__name__)

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
def change(self, patient: "PatientState", context):
    self.state.patient.active = None
    self.state.patient.revision += 1
    self.state.insurance.registrations["1"] = "created"
    self.state.insurance.registrations.pop("1")
    del self.state.insurance.offered
    setattr(self.state.insurance, "write_pending", True)
    active = self.state.patient.active
    active.model_copy(update={})
    patient.absence = None
    context.userdata.patient = None
    self.state.patient.activate(None)
    self.state.patient.mark_appointments_stale()
    if chart := self.state.patient.active:
        chart.appointments.append(None)
    return self.state.insurance.accepted
"""
        found = Scan("scheduling")
        found.visit(ast.parse(source))
        writers = {("PatientState", "active"): {"update_appointments", "activate"}}
        self.assertEqual(
            [
                v.removeprefix("example.py:")
                for v in violations("example.py", found, writers)
            ],
            [
                "3 assigns PatientState.active; call PatientState.update_appointments",
                "4 increments PatientState.revision;"
                " ask PatientResolver in identity.py to change the patient",
                "5 assigns InsuranceState.registrations;"
                " only insurance writes it; move the change there",
                "6 calls pop on InsuranceState.registrations;"
                " only insurance writes it; move the change there",
                "7 deletes InsuranceState.offered;"
                " only insurance writes it; move the change there",
                "8 setattr InsuranceState.write_pending;"
                " only insurance writes it; move the change there",
                "10 copies PatientState.active; call PatientState.update_appointments",
                "11 assigns PatientState.absence;"
                " ask PatientResolver in identity.py to change the patient",
                "12 assigns CallState.patient; only state writes it; move the change there",
                "13 calls PatientState.activate, which only identity may call;"
                " ask PatientResolver in identity.py to change the patient",
                "16 calls append on PatientState.active;"
                " call PatientState.update_appointments",
                "17 reads InsuranceState.accepted; call accepted_insurance(state)",
            ],
        )
