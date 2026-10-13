"""Requested time honored.

Fix for a no: Thinker slot-offering rules. (probation)
"""

QUESTION = {
    "type": "noul",
    "instructions": "When the caller asked for a specific day, date, time of day (morning, afternoon), or the earliest opening, did the agent either offer a matching time from the availability results, or plainly say no matching time was available before suggesting other times? If the caller never asked for a specific day or time, answer true.",
    "criteria": {
        "true": "Every specific day or time the caller asked for was offered from the results, or the agent plainly said none matched.",
        "false": "The caller asked for a day or time and the agent offered other times without offering a matching one or saying none matched.",
    },
}
