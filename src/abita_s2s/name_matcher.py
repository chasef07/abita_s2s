"""The existing agent's first-name and DOB matching policy."""

import re
import unicodedata
from datetime import UTC, date, datetime

from metaphone import doublemetaphone
from rapidfuzz.distance import DamerauLevenshtein


def exact_name(value: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", value).lower())


def normalized_name(value: str) -> str:
    return re.sub(r"(.)\1+", r"\1", exact_name(value))


def names_match(left: str, right: str) -> bool:
    left, right = normalized_name(left), normalized_name(right)
    return bool(
        left
        and right
        and (
            left == right
            or (
                min(len(left), len(right)) >= 3
                and (left.startswith(right) or right.startswith(left))
            )
        )
    )


def phone_name_matches(left: str, right: str) -> bool:
    left, right = normalized_name(left), normalized_name(right)
    if not left or not right:
        return False
    score = DamerauLevenshtein.normalized_similarity(left, right)
    return score >= 0.85 or (
        score >= 0.65
        and bool((set(doublemetaphone(left)) - {""}) & set(doublemetaphone(right)))
    )


def parse_dob(value: str) -> date | None:
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
        return None
    try:
        month, day, year = map(int, value.split("/"))
        parsed = date(year, month, day)
        return parsed if parsed <= datetime.now(UTC).date() else None
    except ValueError:
        return None


def dob_matches(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        match = re.fullmatch(r"(\d{1,2})\D+(\d{1,2})\D+(\d{2,4})", value.strip())
        if not match:
            return re.sub(r"\D", "", value)
        month, day, year = match.groups()
        if len(year) == 2:
            year = ("19" if int(year) > 30 else "20") + year
        return month.zfill(2) + day.zfill(2) + year

    return bool(left and right and normalize(left) == normalize(right))


def first_names(name: str) -> list[str]:
    # AdvancedMD returns both "Surname, First Middle" and natural name order.
    value = name.split(",", 1)[1] if "," in name else name
    value = "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )
    parts = re.findall(r"[A-Za-z]+", value)
    return [parts[0], " ".join(parts if "," in name else parts[:-1])] if parts else []
