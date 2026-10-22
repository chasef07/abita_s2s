"""Trip when a LiveKit release drops, renames, or reshapes a private member the runtime overrides or reads."""

import ast
import importlib
import inspect
import pkgutil
import textwrap
import unittest
from unittest.mock import patch

from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession

import abita_s2s.runtime
from abita_s2s.runtime.observability import ObservedGPTLiveSession

UPSTREAM = inspect.getsourcefile(GPTLiveSession)
OWN_HELPERS = {"_trace_response_event"}
MODULES = [
    importlib.import_module(f"abita_s2s.runtime.{module.name}")
    for module in pkgutil.iter_modules(abita_s2s.runtime.__path__)
] + [importlib.import_module("abita_s2s.model_config")]


def private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def parameters(function) -> list[str]:
    return list(inspect.signature(function).parameters)


def upstream_reads(cls: type) -> set[str]:
    """Private names the class loads off an object but never assigns or defines itself."""
    loaded: set[str] = set()
    owned = set(vars(cls))
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(cls)))):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and private(node.attr)
        ):
            (owned if isinstance(node.ctx, ast.Store) else loaded).add(node.attr)
    return loaded - owned


async def idle(session):
    async for _ in session._msg_ch:
        pass


class LiveKitSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def test_each_override_replaces_a_base_method_with_the_same_parameters(self):
        for name, ours in vars(ObservedGPTLiveSession).items():
            if not private(name) or not callable(ours) or name in OWN_HELPERS:
                continue
            with self.subTest(name=name):
                base = getattr(GPTLiveSession, name, None)
                self.assertIsNotNone(
                    base,
                    f"GPTLiveSession.{name} is gone from {UPSTREAM}; re-diff it and "
                    "move the override in runtime/observability.py",
                )
                self.assertEqual(
                    parameters(ours),
                    parameters(base),
                    f"GPTLiveSession.{name} changed parameters in {UPSTREAM}; re-diff "
                    "it and update the override in runtime/observability.py",
                )

    def test_released_plugin_does_not_trace_yet(self):
        for name in OWN_HELPERS:
            self.assertFalse(
                hasattr(GPTLiveSession, name),
                "LiveKit now ships livekit/agents#7408; delete the port in "
                "runtime/observability.py",
            )

    async def test_each_private_member_read_exists_on_a_live_session(self):
        with patch.object(GPTLiveSession, "_main_task", idle):
            model = GPTLiveModel(api_key="offline")
            live = GPTLiveSession(model)
        self.addAsyncCleanup(model.aclose)
        self.addAsyncCleanup(live.aclose)
        readers = [
            cls
            for module in MODULES
            for cls in vars(module).values()
            if inspect.isclass(cls) and cls.__module__ == module.__name__
        ]
        for reader in readers:
            for name in sorted(upstream_reads(reader)):
                with self.subTest(reader=reader.__name__, name=name):
                    self.assertTrue(
                        hasattr(live, name),
                        f"GPTLiveSession.{name}, read by {reader.__module__}."
                        f"{reader.__name__}, is gone from {UPSTREAM}; re-diff it",
                    )


if __name__ == "__main__":
    unittest.main()
