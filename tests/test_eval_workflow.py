import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class EvalWorkflowTests(unittest.TestCase):
    def test_every_scenario_suite_runs_in_ci(self):
        workflow = (ROOT / ".github/workflows/evals.yml").read_text()
        listed = set(re.findall(r"^\s+(?:- |suite: )(\w+)$", workflow, re.MULTILINE))
        suites = {path.stem for path in (ROOT / "evals/scenarios").glob("*.y*ml")}
        self.assertLessEqual(suites, listed, "Add new scenario suites to evals.yml")


if __name__ == "__main__":
    unittest.main()
