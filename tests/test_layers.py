"""Every module belongs to one layer and imports only the layers it may depend on."""

import ast
import unittest
from importlib.util import resolve_name
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"
TYPE_CHECKING = ("TYPE_CHECKING", "typing.TYPE_CHECKING")

LAYERS = {
    "foundation": (
        "config",
        "insurance_contract",
        "insurance_state",
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
    "owners": {"observability", "tools", "composition", "livekit.agents"},
    "tools": {"integrations", "observability", "composition", "httpx"},
    "composition": set(),
}


def layer_of(name: str) -> str | None:
    return next((layer for layer, names in LAYERS.items() if name in names), None)


def runtime_imports(nodes, package: str):
    """Imported names, resolving relative imports and skipping type-only blocks."""
    for node in nodes:
        if isinstance(node, ast.If) and ast.unparse(node.test) in TYPE_CHECKING:
            yield from runtime_imports(node.orelse, package)
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            yield from (f"{module}.{alias.name}" for alias in node.names)
        yield from runtime_imports(ast.iter_child_nodes(node), package)


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
            for module in runtime_imports(ast.parse(path.read_text()).body, package):
                name = target(module)
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    violations.append(f"{'/'.join(parts)} ({layer}) imports {module}")
        self.assertEqual(violations, [])
