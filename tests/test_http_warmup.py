import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "abita_s2s"

FIRST_REQUESTS_AFTER_PREWARM = """
import asyncio
import http.server
import json
import sys
import threading
from types import SimpleNamespace

import anyio
import anyio.abc
import httpx

from abita_s2s.integrations.transport import tls_context
from abita_s2s.main import prewarm


class Ok(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


prewarm(SimpleNamespace(userdata={}))
tls_ready = tls_context.cache_info().currsize == 1
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
threading.Thread(target=server.serve_forever, daemon=True).start()
lazy = (anyio, anyio.abc)
names_before = [set(vars(module)) for module in lazy]
modules_before = set(sys.modules)


async def first_requests():
    async with httpx.AsyncClient(verify=tls_context()) as client:
        await client.get(f"http://127.0.0.1:{server.server_port}/")
        reused = await client.get(f"http://127.0.0.1:{server.server_port}/")
        try:
            await client.get(f"https://127.0.0.1:{server.server_port}/")
        except httpx.ConnectError:
            return reused.status_code


status = asyncio.run(first_requests())
print(
    json.dumps(
        {
            "tls_ready": tls_ready,
            "status": status,
            "imported": sorted(
                name
                for name in set(sys.modules) - modules_before
                if name.split(".")[0] in ("anyio", "httpcore", "h11", "sniffio")
            ),
            "lazily_resolved": sorted(
                f"{module.__name__}.{name}"
                for module, before in zip(lazy, names_before)
                for name in set(vars(module)) - before
            ),
        }
    )
)
"""


class HttpWarmupTests(unittest.TestCase):
    def test_first_requests_after_prewarm_do_no_http_stack_setup(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GOOGLE_CLOUD_TRACE_")
        }
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", FIRST_REQUESTS_AFTER_PREWARM],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"tls_ready": True, "status": 200, "imported": [], "lazily_resolved": []},
        )

    def test_every_client_takes_the_process_tls_context(self):
        clients, missing = [], []
        for path in sorted(PACKAGE.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "httpx.AsyncClient"
                ):
                    site = f"{path.relative_to(PACKAGE)}:{node.lineno}"
                    clients.append(site)
                    if "verify" not in {keyword.arg for keyword in node.keywords}:
                        missing.append(site)
        self.assertTrue(clients)
        self.assertEqual(missing, [], "pass verify=tls_context() to every client")
