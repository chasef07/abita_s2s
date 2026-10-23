"""Every time budget holds the steps that run inside it, up to the process kill."""

import unittest

from abita_s2s import budgets
from abita_s2s.budgets import PROCESS_KILL, Budget


def nested(budget: Budget):
    yield budget
    for step in budget.steps:
        yield from nested(step)


def overruns(root: Budget) -> tuple[list[str], list[str]]:
    """One line per budget that holds steps, and the lines whose steps overrun it."""
    lines, failures = [], []
    for budget in nested(root):
        if not budget.steps:
            continue
        total = sum(step.seconds for step in budget.steps)
        line = (
            " + ".join(f"{step.name} {step.seconds:g}s" for step in budget.steps)
            + f" = {total:g}s inside {budget.name} {budget.seconds:g}s"
        )
        lines.append(line)
        if total > budget.seconds:
            failures.append(line)
    return lines, failures


class BudgetTests(unittest.TestCase):
    def test_each_budget_fits_inside_the_next(self):
        lines, failures = overruns(PROCESS_KILL)
        print("\n".join(["", *lines]))
        self.assertEqual(failures, [], "steps overrun the budget they run inside")

    def test_every_budget_runs_inside_the_process_kill(self):
        declared = {
            value.name for value in vars(budgets).values() if isinstance(value, Budget)
        }
        self.assertEqual(
            declared - {budget.name for budget in nested(PROCESS_KILL)},
            set(),
            "add each budget to the steps of the budget it runs inside",
        )


if __name__ == "__main__":
    unittest.main()
