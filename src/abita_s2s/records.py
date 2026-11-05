"""Strict, frozen wire records shared by owners, contracts and adapters."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


def provider_name(name: str) -> str:
    """The name the caller hears for a provider, however the chart spells it."""
    for old, new in (
        ("Dr. Austin Bach (Overflow)", "Dr. Bach"),
        ("Dr. Austin Bach", "Dr. Bach"),
        ("Dr. J. Licht", "Dr. Licht"),
        ("Dr. D. Noel", "Dr. Noel"),
    ):
        name = name.replace(old, new)
    return name


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    def __repr_args__(self):
        return ()
