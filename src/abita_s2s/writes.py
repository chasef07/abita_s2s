"""One ledger for every external write: a key per write and one table from outcome to effect.

Outcomes of a sent write:
    written      the change is confirmed.
    partial      part of the change is confirmed; staff must finish it.
    uncertain    the change may or may not have happened.
    refused      the middleware refused the request; the target record is unchanged.
    expired      the middleware refused the target's token; the target record is unchanged.
    failed       the middleware could not write; the target record is unchanged.
    unsent       the request never reached the record.
    needs_input  the middleware needs more facts; nothing changed.
"""

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Literal, assert_never

WriteOutcome = Literal[
    "written",
    "partial",
    "uncertain",
    "refused",
    "expired",
    "failed",
    "unsent",
    "needs_input",
]
WriteAction = Literal["book", "cancel", "reschedule", "register", "update_insurance"]


@dataclass(frozen=True)
class WriteKey:
    """Whose record a write changes, the action, and what it acts on."""

    subject: Hashable
    action: WriteAction
    target: Hashable = None


@dataclass(frozen=True)
class WriteEffect:
    """What an outcome does after the write settles.

    replays: the same write answers from its receipt instead of sending again.
    blocks: every later write for the subject answers from this receipt.
    spends_target: the loaded record the write acted on must be reloaded first.
    """

    replays: bool
    blocks: bool
    spends_target: bool


def write_effect(outcome: WriteOutcome) -> WriteEffect:
    match outcome:
        case "written":
            return WriteEffect(replays=True, blocks=False, spends_target=False)
        case "partial" | "uncertain":
            return WriteEffect(replays=True, blocks=True, spends_target=False)
        case "refused" | "expired" | "failed":
            return WriteEffect(replays=False, blocks=False, spends_target=True)
        case "unsent" | "needs_input":
            return WriteEffect(replays=False, blocks=False, spends_target=False)
        case _:
            assert_never(outcome)


@dataclass(frozen=True)
class Recorded[R]:
    key: WriteKey
    outcome: WriteOutcome
    receipt: R


class WriteLedger[R]:
    """Receipts of sent writes, in the order they settled. write_effect decides what stays."""

    def __init__(self) -> None:
        self._writes: dict[WriteKey, Recorded[R]] = {}

    def record(self, key: WriteKey, outcome: WriteOutcome, receipt: R) -> None:
        self._writes.pop(key, None)
        if write_effect(outcome).replays:
            self._writes[key] = Recorded(key, outcome, receipt)

    def get(self, key: WriteKey) -> R | None:
        recorded = self._writes.get(key)
        return recorded.receipt if recorded else None

    def receipts(self, subject: Hashable) -> list[R]:
        return [w.receipt for w in self._writes.values() if w.key.subject == subject]

    def blocking(self, subject: Hashable) -> R | None:
        """The receipt that answers every later write for the subject, if any."""
        return next(
            (
                w.receipt
                for w in self._writes.values()
                if w.key.subject == subject and write_effect(w.outcome).blocks
            ),
            None,
        )
