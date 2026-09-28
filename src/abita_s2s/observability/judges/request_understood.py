"""Request understood."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent correctly understand what the caller wanted, including corrections and changes during the call? Judge understanding separately from whether tools succeeded.",
    "criteria": {
        "true": "The agent understood and addressed the caller's actual requests and final corrections.",
        "false": "The agent misunderstood, ignored a correction, or pursued a different request.",
    },
}
