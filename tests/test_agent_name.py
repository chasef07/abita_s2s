import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from abita_s2s.config import agent_name

DOCKERFILE = (Path(__file__).parents[1] / "Dockerfile").read_text().splitlines()
IMAGE_ENV = dict(
    pair.split("=", 1)
    for line in DOCKERFILE
    if line.startswith("ENV ")
    for pair in line.split()[1:]
)


class AgentNameTests(unittest.TestCase):
    def test_deployed_image_registers_as_production(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from abita_s2s.main import server; print(server._agent_name)",
                ],
                cwd=directory,
                env={**os.environ, "LIVEKIT_AGENT_NAME_OVERRIDE": "", **IMAGE_ENV},
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertEqual(result.stdout.strip(), "abita-s2s")

    def test_local_worker_registers_under_developer_name(self):
        with patch.dict("os.environ", {"LOGNAME": "dev"}, clear=True):
            self.assertEqual(agent_name(), "abita-s2s-local-dev")

    def test_unknown_deployed_marker_fails(self):
        with (
            patch.dict("os.environ", {"ABITA_S2S_DEPLOYED": "true"}, clear=True),
            self.assertRaises(ValueError),
        ):
            agent_name()
