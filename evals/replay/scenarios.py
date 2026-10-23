"""The fixed caller scripts and the checks each call must pass."""

from dataclasses import dataclass

from evals.replay.checks import (
    Check,
    delegates,
    ends_call,
    first_argument,
    grounded,
    never_says,
    no_unstarted_action,
)

SEARCH = "search_office_knowledge"
ADDRESS = r"\d|\b(street|avenue|way|road|boulevard|drive|highway|suite|calle|avenida)\b"
HOURS = r"\d|\b(nine|four|eight|five|seven|nueve|cuatro|ocho|cinco)\b"
OPEN_OR_CLOSED = r"\b(we're|we are|estamos) (open|closed|not open|abiertos|cerrados)|\bclosed\b|\bcerrado"
AGREES = r"\b(yes|yeah|yep|correct|exactly|that's right|sí|así es|correcto)\b"
ACCEPTS = (
    AGREES
    + r"|(?<!whether )(?<!if )\bwe (do )?(accept|take) (it|that|ambetter|aetna)"
    + r"|\b(is|are) accepted\b|\bin network\b|(?<!si )\baceptamos\b"
)
APPOINTMENT = r"\b(november|noviembre|12th|twelfth|10:30|ten thirty)\b"
INVENTED_PARKING = (
    r"\b(free|plenty of|ample|street|garage|lot|on-site) parking\b"
    r"|\bparking (lot|garage|is (free|available)|available)\b|\byou can park\b"
    r"|\bestacionamiento (gratis|gratuito|disponible)\b"
)
WRONG_FACTS = (
    r"(?:^|[.!?])(?![^.!?]*(?:\b(?:not|no|actually|instead)\b|n['’]t\b))[^.!?]*"
    r"(?:\bmain street\b|\b(?:close|closes|open until|until) (?:at )?(?:seven|7)\b)"
)
RULES_OUT_TRANSFER = r"\b(can't|cannot|can not|unable to|not able to|no puedo) (transfer|connect|put you through|transferir|comunicar)"
STAFF_AVAILABILITY = (
    r"\b(she|whitney)(?:'s| is) (available|busy|in|out|here|not here|with a patient|at lunch|in a meeting|on another call)\b"
    r"|\b(está|esta) (disponible|ocupada|en una reunión)\b"
)
PROMISED_CALLBACK = (
    r"\b(she|whitney|someone|they)(?:'ll| will) (call you back|get back to you|reach out|be in touch|return your call)"
    r"|\bI(?:'ll| will) (let her know|tell her|pass (it|that|the message|this) (on|along))"
)


@dataclass(frozen=True)
class Scenario:
    """One scripted call; each turn holds the caller's breaths, spoken back to back."""

    name: str
    mirrors: str
    office: str
    turns: tuple[tuple[str, ...], ...]
    checks: tuple[Check, ...]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "address_first",
        "office_facts",
        "spring-hill",
        (
            ("Hi, what's your address?",),
            ("And is there parking there?",),
            ("Thanks, bye.",),
        ),
        (
            delegates(SEARCH, 0),
            grounded(SEARCH, ADDRESS, "an address"),
            never_says(INVENTED_PARKING, "invented a parking detail"),
            no_unstarted_action(),
            ends_call(2),
        ),
    ),
    Scenario(
        "wrong_facts_suggested",
        "office_facts",
        "spring-hill",
        (
            ("You close at seven on Fridays, right? And you're on Main Street?",),
            ("Okay, thanks. Bye.",),
        ),
        (
            delegates(SEARCH, 0),
            grounded(
                SEARCH, AGREES + "|" + HOURS + r"|\bmain street\b", "an office fact"
            ),
            never_says(WRONG_FACTS, "confirmed a wrong fact"),
            no_unstarted_action(),
            ends_call(1),
        ),
    ),
    Scenario(
        "thanks_then_question",
        "call_ending",
        "spring-hill",
        (
            ("What are your hours on Monday?",),
            (
                "Okay, thank you.",
                "Oh, um, do I need to bring anything for a new patient visit?",
            ),
            ("Great, that's all. Bye.",),
        ),
        (
            delegates(SEARCH, 0),
            grounded(SEARCH, HOURS, "hours"),
            no_unstarted_action(),
            ends_call(2),
        ),
    ),
    Scenario(
        "goodbye_two_breaths",
        "call_ending",
        "spring-hill",
        (("Are you open on Saturdays?",), ("Okay, thank you so much.", "Bye-bye.")),
        (
            grounded(SEARCH, OPEN_OR_CLOSED, "whether the office is open"),
            no_unstarted_action(),
            ends_call(1),
        ),
    ),
    Scenario(
        "chart_lookup",
        "delegation_stall",
        "spring-hill",
        (
            (
                "Hi, I'm an existing patient. I think I have an appointment coming up "
                "but I forgot the day. My name is Lana, L-A-N-A, and my date of birth "
                "is April sixteenth, 1992.",
            ),
            ("Okay, thank you. Bye.",),
        ),
        (
            delegates("resolve_patient", 0),
            grounded("resolve_patient", APPOINTMENT, "the appointment"),
            no_unstarted_action(),
            ends_call(1),
        ),
    ),
    Scenario(
        "plan_and_visit",
        "delegation_stall",
        "spring-hill",
        (
            (
                "Hi, do you take Ambetter Balanced Care from the marketplace? I want an "
                "eye exam for new glasses. Things have been getting blurry little by little.",
            ),
            ("Okay, great. Thanks, bye.",),
        ),
        (
            delegates("check_insurance", 0),
            first_argument("check_insurance", "coverageType", "routine_vision"),
            grounded("check_insurance", ACCEPTS, "plan acceptance"),
            no_unstarted_action(),
            ends_call(1),
        ),
    ),
    Scenario(
        "pediatrician_referral",
        "#173 triage",
        "spring-hill",
        (
            (
                "Hi, do you take Aetna? My son's pediatrician did a vision screening and "
                "said he's not seeing well and needs an eye exam. He's eight.",
            ),
            ("No, it's been gradual. He just can't read the board at school.",),
            ("Okay, thanks. Bye.",),
        ),
        (
            first_argument("check_insurance", "coverageType", "routine_vision"),
            grounded("check_insurance", ACCEPTS, "plan acceptance"),
            no_unstarted_action(),
            ends_call(2),
        ),
    ),
    Scenario(
        "spanish_small_print",
        "#173 triage",
        "spring-hill",
        (
            (
                "Hola, ¿aceptan Aetna? Quiero un examen de la vista. No veo bien las "
                "letras pequeñas y ha empeorado poco a poco.",
            ),
            ("Gracias, adiós.",),
        ),
        (
            first_argument("check_insurance", "coverageType", "routine_vision"),
            no_unstarted_action(),
            ends_call(1),
        ),
    ),
    Scenario(
        "person_request",
        "delegation_stall",
        "spring-hill",
        (("Hi, can I talk to someone at the front desk, please?",),),
        (
            no_unstarted_action(),
            grounded("transfer_call", RULES_OUT_TRANSFER, "a ruled-out transfer"),
        ),
    ),
    Scenario(
        "named_staff",
        "#153 narration",
        "crystal-river",
        (
            ("Hi, is Whitney there? I'm returning her call.",),
            ("I don't know what it's about.",),
        ),
        (
            no_unstarted_action(),
            never_says(STAFF_AVAILABILITY, "claimed staff availability"),
            never_says(PROMISED_CALLBACK, "promised a callback"),
            grounded("transfer_call", RULES_OUT_TRANSFER, "a ruled-out transfer"),
        ),
    ),
)
