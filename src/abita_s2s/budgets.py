"""Every time budget on the transfer and shutdown paths, and the steps each one must hold."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    """Seconds one step may take, and the steps that run one after another inside it."""

    name: str
    seconds: float
    steps: tuple["Budget", ...] = ()


ANNOUNCEMENT = Budget("transfer announcement", 10)
SIP_RINGING = Budget("SIP ringing", 20)
SIP_TRANSFER = Budget("SIP transfer request", 30, (SIP_RINGING,))
TRANSFER = Budget("transfer", 40, (ANNOUNCEMENT, SIP_TRANSFER))
DRAIN = Budget("write drain", 50, (TRANSFER,))
SEND = Budget("closeout send attempt", 5)
RETRY_PAUSE = Budget("closeout retry pause", 0.25)
CLOSEOUT = Budget(
    "closeout",
    2 * SEND.seconds + RETRY_PAUSE.seconds + 1,
    (SEND, RETRY_PAUSE, SEND),
)
FINISH = Budget("reporter finish", 80, (DRAIN, CLOSEOUT))
TRANSPORT_CLOSE = Budget("transport close", 5)
PROCESS_KILL = Budget(
    "process shutdown",
    FINISH.seconds + TRANSPORT_CLOSE.seconds + 5,
    (FINISH, TRANSPORT_CLOSE),
)
