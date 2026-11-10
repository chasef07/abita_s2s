"""Every module belongs to one layer and imports only the layers it may depend on."""

import ast
import re
import unittest
from importlib.util import resolve_name
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"
TYPE_CHECKING = ("TYPE_CHECKING", "typing.TYPE_CHECKING")
TYPE_ONLY = {
    "abita_s2s.insurance_list.OfficePlans",
    "abita_s2s.runtime.reporting.CallReporter",
}

LAYERS = {
    "foundation": (
        "budgets",
        "call_record",
        "config",
        "insurance_contract",
        "name_matcher",
        "offices",
        "prompt",
        "records",
        "results",
        "state",
    ),
    "integrations": ("integrations",),
    "observability": ("observability",),
    "owners": (
        "call_control",
        "handoff",
        "identity",
        "insurance",
        "insurance_list",
        "knowledge",
        "scheduling",
        "staff_tasks",
    ),
    "tools": ("tools",),
    "composition": ("agent", "main", "model_config", "release", "runtime"),
}

FORBIDDEN = {
    "foundation": {
        "integrations",
        "observability",
        "owners",
        "tools",
        "composition",
        "livekit.agents",
    },
    "integrations": {
        "observability",
        "owners",
        "tools",
        "composition",
        "livekit.agents",
    },
    "observability": {"integrations", "owners", "tools", "composition"},
    "owners": {"observability", "tools", "composition", "livekit.agents", "httpx"},
    "tools": {"integrations", "observability", "composition", "httpx"},
    "composition": set(),
}


FIXES = {"httpx": "send HTTP through an adapter in integrations/"}


def layer_of(name: str) -> str | None:
    return next((layer for layer, names in LAYERS.items() if name in names), None)


def checked_imports(nodes, package: str):
    """Imported names, resolving relative imports. Only TYPE_ONLY may skip the layers."""
    for node in nodes:
        if isinstance(node, ast.If) and ast.unparse(node.test) in TYPE_CHECKING:
            yield from (
                name
                for name in checked_imports(node.body, package)
                if name not in TYPE_ONLY
            )
            yield from checked_imports(node.orelse, package)
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            yield from (f"{module}.{alias.name}" for alias in node.names)
        yield from checked_imports(ast.iter_child_nodes(node), package)


def target(module: str) -> str:
    if module == "abita_s2s" or not module.startswith("abita_s2s."):
        return module
    return layer_of(module.split(".")[1]) or module


class LayerTests(unittest.TestCase):
    def test_every_layer_entry_exists(self):
        for names in LAYERS.values():
            for name in names:
                path = PACKAGE / name
                self.assertTrue(path.is_dir() or path.with_suffix(".py").exists(), name)

    def test_modules_import_only_allowed_layers(self):
        violations = []
        for path in sorted(PACKAGE.rglob("*.py")):
            parts = path.relative_to(PACKAGE).with_suffix("").parts
            if parts == ("__init__",):
                continue
            layer = layer_of(parts[0])
            self.assertIsNotNone(layer, f"Assign {parts[0]} to a layer in LAYERS")
            package = ".".join(("abita_s2s", *parts[:-1]))
            forbidden = FORBIDDEN[layer]
            for module in checked_imports(ast.parse(path.read_text()).body, package):
                name = target(module)
                for f in forbidden:
                    if name == f or name.startswith(f + "."):
                        violations.append(
                            f"{'/'.join(parts)} ({layer}) imports {module}"
                            + (f"; {FIXES[f]}" if f in FIXES else "")
                        )
        self.assertEqual(violations, [])

    def test_type_only_exemptions_are_still_imported(self):
        sources = "".join(path.read_text() for path in PACKAGE.rglob("*.py"))
        for name in TYPE_ONLY:
            module, _, imported = name.rpartition(".")
            self.assertRegex(
                sources, rf"from {re.escape(module)} import [^\n]*\b{imported}\b"
            )
