"""Strict, frozen wire records shared by owners, contracts and adapters."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    def __repr_args__(self):
        return ()
