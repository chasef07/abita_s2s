"""One call's owners for a test, with stand-ins for the owners it does not exercise."""

from unittest.mock import Mock

from abita_s2s.offices import SPRING_HILL
from abita_s2s.tools.registry import Owners


def call_owners(office=SPRING_HILL, **given) -> Owners:
    scheduling = Mock()
    scheduling.appointments_text.return_value = ""
    stand_ins = {
        "resolver": Mock(),
        "scheduling": scheduling,
        "insurance": Mock(),
        "staff_tasks": Mock(),
        "call_control": Mock(),
    }
    return Owners(office, **(stand_ins | given))
