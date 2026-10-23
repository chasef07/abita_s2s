"""Deterministic verdicts over a replayed call: what the caller said, what the agent said, and which tools ran."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    turn: int
    text: str


@dataclass(frozen=True)
class Agent:
    text: str


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict[str, object]
    call_id: str


@dataclass(frozen=True)
class Output:
    name: str
    text: str
    call_id: str


Event = Caller | Agent | Call | Output
Transcript = Sequence[Event]
Check = Callable[[Transcript], str | None]

CLAIM = re.compile(
    r"\blet me (check|look|see|pull|find|confirm|verify|get|transfer|connect)"
    r"|\bI(?:'m| am) (checking|looking|pulling|searching|transferring|connecting|"
    r"booking|scheduling|cancell?ing|rescheduling|noting|sending|putting|adding|"
    r"registering|verifying|confirming)"
    r"|\bI(?:'ll| will) (check|look|see|find out|pull|search|transfer|connect|book|"
    r"schedule|cancel|reschedule|note|send|put you|add|register|verify|confirm|"
    r"get (her|him|them|someone)(?=[.,!]|$| on the| for you))"
    r"|\b(transferring|connecting) you|\bbooking (that|it|this|you)"
    r"|\bchecking (your|on|the|that|this|if|whether|who)"
    r"|\b(d[ée]j[ae]me|perm[ií]t[ae]me) (revisar|verificar|ver|buscar|confirmar|"
    r"consultar|transferir|comunicar)"
    r"|\b(estoy|voy a) (revisando|verificando|buscando|consultando|transfiriendo|"
    r"comunicando|agendando|revisar|verificar|buscar|consultar|transferir|"
    r"comunicar|agendar|ver)"
    r"|\b(le|lo|la) (transfiero|comunico)",
    re.IGNORECASE,
)
TRANSFER = re.compile(r"transf|connect|comunic|get (her|him|them|someone)", re.I)
LEAVING = frozenset({"transfer_call", "end_call"})
SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def _window(events: Transcript, at: int) -> tuple[int, int]:
    """The index range around ``at`` from the caller line before it, or the call start, to the next caller line."""
    heard = [i for i, e in enumerate(events) if isinstance(e, Caller)]
    start = max((i for i in heard if i <= at), default=0)
    end = min((i for i in heard if i > at), default=len(events))
    return start, end


def _started(events: Transcript, at: int, transfer: bool) -> bool:
    """Whether the claimed kind of tool ran when the agent spoke at ``at``, or ran after it before the caller spoke again.

    A transfer claim needs ``transfer_call``. Any other claim needs a tool that does the
    work, so a transfer or hang-up behind "let me check" does not count.
    """
    start, end = _window(events, at)
    finished = {e.call_id for e in events[start:at] if isinstance(e, Output)}
    return any(
        isinstance(e, Call)
        and (e.name == "transfer_call" if transfer else e.name not in LEAVING)
        and (i > at or e.call_id not in finished)
        for i, e in enumerate(events[start:end], start)
    )


def _after_first_caller(events: Transcript) -> list[tuple[int, Agent]]:
    first = next((i for i, e in enumerate(events) if isinstance(e, Caller)), None)
    if first is None:
        return []
    return [(i, e) for i, e in enumerate(events) if i > first and isinstance(e, Agent)]


def no_unstarted_action() -> Check:
    """Every action the agent says it is doing has a tool behind it before the caller speaks again (#153)."""

    def check(events: Transcript) -> str | None:
        for i, said in _after_first_caller(events):
            for sentence in SENTENCE.findall(said.text):
                claim = CLAIM.search(sentence)
                offers = sentence.rstrip().endswith("?")
                if claim is None or offers:
                    continue
                transfer = TRANSFER.search(claim.group(0)) is not None
                if not _started(events, i, transfer):
                    return f"narrated an unstarted action: {said.text.strip()!r}"
        return None

    return check


def delegates(tool: str, turn: int) -> Check:
    """``tool`` runs after caller turn ``turn`` and before the caller has to speak again."""

    def check(events: Transcript) -> str | None:
        heard = [(i, e.turn) for i, e in enumerate(events) if isinstance(e, Caller)]
        start = next((i for i, t in heard if t == turn), None)
        if start is None:
            return f"the call never reached caller turn {turn}"
        end = next((i for i, t in heard if t > turn), len(events))
        if any(isinstance(e, Call) and e.name == tool for e in events[start:end]):
            return None
        return f"stalled: no {tool} after caller turn {turn}"

    return check


def grounded(tool: str, pattern: str, what: str) -> Check:
    """After the greeting, the agent says nothing matching ``pattern`` before the first ``tool`` call."""
    found = re.compile(pattern, re.IGNORECASE)

    def check(events: Transcript) -> str | None:
        first = next(
            (i for i, e in enumerate(events) if isinstance(e, Call) and e.name == tool),
            len(events),
        )
        for i, said in _after_first_caller(events):
            if i < first and found.search(said.text):
                return f"stated {what} before {tool}: {said.text.strip()!r}"
        return None

    return check


def never_says(pattern: str, what: str) -> Check:
    found = re.compile(pattern, re.IGNORECASE)

    def check(events: Transcript) -> str | None:
        for _, said in _after_first_caller(events):
            if found.search(said.text):
                return f"{what}: {said.text.strip()!r}"
        return None

    return check


def ends_call(after_turn: int) -> Check:
    """``end_call`` runs, and only after the caller's final turn ``after_turn`` began."""

    def check(events: Transcript) -> str | None:
        last = next(
            (
                i
                for i, e in enumerate(events)
                if isinstance(e, Caller) and e.turn == after_turn
            ),
            None,
        )
        ended = next(
            (
                i
                for i, e in enumerate(events)
                if isinstance(e, Call) and e.name == "end_call"
            ),
            None,
        )
        if last is None:
            return f"ended the call before caller turn {after_turn}"
        if ended is None:
            return "did not end the call after the caller's goodbye"
        if ended < last:
            return f"ended the call before caller turn {after_turn}"
        return None

    return check


def first_argument(tool: str, name: str, value: object) -> Check:
    """The first ``tool`` call passes ``name=value``."""

    def check(events: Transcript) -> str | None:
        call = next((e for e in events if isinstance(e, Call) and e.name == tool), None)
        if call is None:
            return f"never called {tool}"
        if call.arguments.get(name) != value:
            return f"{tool} {name} was {call.arguments.get(name)!r}, expected {value!r}"
        return None

    return check
