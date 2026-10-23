"""Every time budget outlasts the steps that run inside it, up to the process kill."""

import unittest

from abita_s2s import budgets
from abita_s2s.budgets import PROCESS_KILL, TRANSFER, Budget

SHARED_DEADLINE = {TRANSFER.name}
PROCESS_KILL_CEILING_SECONDS = 90


def nested(budget: Budget):
    yield budget
    for step in budget.steps:
        yield from nested(step)


def overruns(root: Budget) -> tuple[list[str], list[str]]:
    """One line per budget that holds steps, and the lines that leave it no slack.

    A budget in ``SHARED_DEADLINE`` is the deadline its last step's own timeout also
    ends on, so its steps may fill it exactly.
    """
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
        if budget.name in SHARED_DEADLINE:
            if total > budget.seconds:
                failures.append(line)
        elif total >= budget.seconds:
            failures.append(line)
    return lines, failures


class BudgetTests(unittest.TestCase):
    def test_each_budget_outlasts_the_steps_inside_it(self):
        lines, failures = overruns(PROCESS_KILL)
        print("\n".join(["", *lines]))
        self.assertEqual(
            failures,
            [],
            "each budget must outlast its steps so they end on their own timeouts",
        )

    def test_process_kill_stays_within_the_shutdown_finish_was_sized_for(self):
        self.assertLessEqual(
            PROCESS_KILL.seconds,
            PROCESS_KILL_CEILING_SECONDS,
            "process shutdown is derived from reporter finish; a longer finish holds "
            "every deploy longer, so shorten a step instead of raising this ceiling",
        )

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
